"""
JellyfinAdapter: implementation of
:class:`services.adapters.MediaServerAdapter` against the Jellyfin REST
API.

API references used while building this adapter:
- https://api.jellyfin.org/ (canonical OpenAPI browser)
- https://jellyfin-jellyfin.mintlify.app/ (rendered docs)
- https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f
  (authoritative MediaBrowser Authorization header doc)
- https://jmshrv.com/posts/jellyfin-api/ (broad overview)

Capability notes
----------------
- **Authentication**: ``POST /Users/AuthenticateByName`` with the
  ``MediaBrowser`` Authorization scheme. The adapter is constructed
  with an already-issued access token (end user pastes it from the
  Jellyfin Dashboard API Keys page, or it's exchanged via
  ``authenticate_by_name``).
- **Admin impersonation**: admin tokens can write per-user state via
  ``UserId`` in the URL. Per-user tokens are not required for writes;
  the adapter passes ``UserContext.backend_user_id`` directly into the
  endpoint path.
- **Ratings**: Jellyfin exposes both a numeric ``UserData.Rating`` and
  a binary ``IsFavorite``. ``set_rating`` writes the numeric value;
  ``set_favorite`` writes the binary toggle. The cross-backend rating
  mapping decision (D-RATE) lives in the engine, not the adapter.
- **User management**: ``create_user`` / ``set_user_password`` /
  ``delete_user`` all supported (used by the D-OWNER user-creation
  modal in PR-Backends frontend work).
- **Position units**: Jellyfin uses 100-nanosecond ticks for
  ``PlaybackPositionTicks`` (1ms = 10,000 ticks). The adapter
  converts at the boundary so the engine always works in milliseconds.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional

from services.guid_translator import normalize_guids

from . import (
    CollectionSpec,
    ItemRef,
    ItemSnapshot,
    LibrarySpec,
    MediaServerAdapter,
    PlaylistSpec,
    ServerIdentity,
    UserContext,
    UserPolicy,
    UserSpec,
    WriteResult,
)
from ._http_base import AuthCredentials, HttpMediaAdapterMixin, make_session


log = logging.getLogger("plexmigrate.services.adapters.jellyfin")


# Conversion factor: Jellyfin PlaybackPositionTicks are 100-nanosecond
# units. 10_000 ticks = 1 millisecond.
_TICKS_PER_MS = 10_000


# Jellyfin item Type values that count as leaf media items for our
# enumeration purposes. Containers (Series, Season, MusicAlbum, BoxSet,
# Playlist) are excluded by default; the engine snapshots leaf state
# (per-episode watch counts, per-track ratings) and reads container
# membership through dedicated endpoints.
#
# v0.16 expansion: audiobook, book, and photo libraries are now
# captured. AudioBook + Book leaves cover Jellyfin's "Books" library
# CollectionType; Photo covers "Photos". MusicVideo is the leaf type
# for the "musicvideos" library; we accept it alongside Audio so a
# mixed-media library doesn't lose track of music videos.
_LEAF_ITEM_TYPES = "Movie,Episode,Audio,MusicVideo,AudioBook,Book,Photo"

# Per-library override: when ``iter_items`` knows the library type
# from a prior ``list_libraries`` call (passed by the engine), we
# narrow ``IncludeItemTypes`` to just the relevant leaf so the
# response doesn't carry irrelevant items mixed in (rare today but
# defensive). The engine doesn't pass library_type to iter_items
# today; this map is documentation for the count-helper which IS
# library_type-aware.
_LEAF_TYPES_BY_LIBRARY_TYPE = {
    "movie": "Movie",
    "show": "Episode",
    "artist": "Audio",
    "musicvideo": "MusicVideo",
    "audiobook": "AudioBook,Book",
    "book": "Book",
    "photo": "Photo",
}


# Provider-id key -> normalized GUID scheme. Mirrors what
# services/guid_translator.py canonicalises Plex agents into.
_PROVIDER_TO_GUID = {
    "Imdb": "imdb",
    "Tmdb": "tmdb",
    "Tvdb": "tvdb",
    "MusicBrainzAlbum": "mbalbum",
    "MusicBrainzAlbumArtist": "mbartist",
    "MusicBrainzArtist": "mbartist",
    "MusicBrainzTrack": "mbtrack",
    "MusicBrainzReleaseGroup": "mbreleasegroup",
}


class JellyfinAdapter(HttpMediaAdapterMixin, MediaServerAdapter):
    """Jellyfin implementation of :class:`MediaServerAdapter`.

    Constructed with the Jellyfin server's base URL and an access
    token (admin API key, typically). The adapter holds its own
    ``requests.Session`` configured with the shared retry adapter and
    telemetry hook from ``services/auth.py``.

    The owner / admin user is identified by ``GET /Users/Me``
    (resolved against the supplied token); this populates
    ``server_identity().owner_user_id``."""

    backend: str = "jellyfin"
    _auth_scheme: str = "jellyfin"  # AuthCredentials backend flag

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        *,
        owner_user_id: Optional[str] = None,
        machine_id: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        # The admin's own Jellyfin user-id, used as the implicit
        # UserContext for ``iter_items(user_context=None)`` calls and
        # as the default for playlist / collection write paths that
        # need a UserId in the URL.
        self._owner_user_id = (owner_user_id or "").strip()
        self._machine_id_cached = (machine_id or "").strip()
        self._creds = AuthCredentials(
            backend=self._auth_scheme,
            token=admin_token,
            # Jellyfin doesn't carry UserId in the header (Emby does).
            user_id=None,
        )
        self._session = make_session(self._creds)
        # Cached on first use to keep ``server_identity`` cheap.
        self._identity_cache: Optional[ServerIdentity] = None

    # ── Discovery ──────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Cheap liveness check via ``GET /System/Info/Public`` (no
        auth required). Returns True on 2xx."""
        try:
            resp = self._session.get(
                self._url("/System/Info/Public"), timeout=10,
            )
            return resp.status_code == 200
        except Exception as exc:
            log.debug("ping failed: %s", exc)
            return False

    def server_identity(self) -> ServerIdentity:
        if self._identity_cache is not None:
            return self._identity_cache
        try:
            info = self._get_json("/System/Info")
        except Exception as exc:
            log.warning("server_identity: /System/Info failed: %s", exc)
            info = {}
        # Resolve owner via /Users/Me (token's user). On a pure API-key
        # auth there's no associated user; fall back to listing users
        # and finding the first admin.
        owner_user_id = self._owner_user_id
        owner_display = ""
        try:
            me = self._get_json("/Users/Me")
            owner_user_id = str(me.get("Id") or owner_user_id)
            owner_display = str(me.get("Name") or "")
        except Exception:
            # /Users/Me 401s for pure API-key auth; find an admin user.
            if not owner_user_id:
                for u in self._safe_list_users():
                    if u.is_admin:
                        owner_user_id = u.backend_user_id
                        owner_display = u.display_name
                        break

        ident = ServerIdentity(
            machine_id=str(info.get("Id") or self._machine_id_cached or ""),
            name=str(info.get("ServerName") or ""),
            version=str(info.get("Version") or ""),
            owner_user_id=owner_user_id or "",
            owner_display=owner_display or "Jellyfin Owner",
        )
        self._identity_cache = ident
        if owner_user_id and not self._owner_user_id:
            self._owner_user_id = owner_user_id
        return ident

    def list_libraries(self) -> List[LibrarySpec]:
        """Walks ``GET /Library/VirtualFolders`` for the library list,
        then issues one cheap ``GET /Users/{id}/Items?ParentId=<lib>&Limit=0``
        per library so the end user-facing catalogue shows real item
        counts (matches Plex's ``section.totalSize`` behavior).

        The per-library count adds one HTTP round-trip per library
        (so ~3-6 extra calls for a typical home setup). The probe
        path already pays one round-trip for VirtualFolders; the
        extra cost is acceptable in exchange for non-zero counts in
        the UI."""
        try:
            payload = self._get_json("/Library/VirtualFolders")
        except Exception as exc:
            log.warning("list_libraries: VirtualFolders failed: %s", exc)
            return []
        # Ensure we have an owner user id for the per-library count
        # query. Some installs require auth on /Users/{id}/Items even
        # for admin tokens; resolve owner identity if we don't yet
        # have it.
        if not self._owner_user_id:
            try:
                self.server_identity()
            except Exception:
                pass
        uid = self._owner_user_id
        out: List[LibrarySpec] = []
        for entry in payload or []:
            library_id = str(entry.get("ItemId") or entry.get("Id") or "")
            lib_type = _normalise_collection_type(entry.get("CollectionType"))
            count: Optional[int] = None
            if library_id and uid:
                count = self._count_library_items(uid, library_id, lib_type)
            out.append(LibrarySpec(
                library_id=library_id,
                name=str(entry.get("Name") or ""),
                type=lib_type,
                item_count=count,
            ))
        return out

    def _count_library_items(
        self, uid: str, library_id: str, lib_type: str,
    ) -> Optional[int]:
        """Cheap count of leaf items in one library. ``Limit=0``
        returns the ``TotalRecordCount`` without paginating any items.
        Returns None on any failure so the UI renders a soft fallback.

        ``IncludeItemTypes`` is per-library: a movie library counts
        Movie, a TV library counts Episode (the leaves; matches Plex
        ``section.totalSize``), a music library counts Audio. For
        unknown / mixed library types we fall back to a generic leaf
        set so the count is at least non-zero when items exist."""
        leaf_types_by_lib = {
            "movie": "Movie",
            "show": "Episode",
            "artist": "Audio",
            "audiobook": "AudioBook,Book",
            "book": "Book",
            "boxsets": "BoxSet",
            "photo": "Photo",
        }
        include_types = leaf_types_by_lib.get(
            lib_type, "Movie,Episode,Audio,AudioBook,Book",
        )
        try:
            data = self._get_json(
                f"/Users/{uid}/Items",
                params={
                    "ParentId": library_id,
                    "Recursive": "true",
                    "IncludeItemTypes": include_types,
                    "Limit": 0,
                    "EnableTotalRecordCount": "true",
                },
            )
        except Exception as exc:
            log.debug(
                "library count failed for %s/%s: %s",
                library_id, lib_type, exc,
            )
            return None
        if isinstance(data, dict):
            try:
                return int(data.get("TotalRecordCount") or 0)
            except (TypeError, ValueError):
                return None
        return None

    def list_users(self) -> List[UserSpec]:
        return self._safe_list_users()

    def _safe_list_users(self) -> List[UserSpec]:
        try:
            payload = self._get_json("/Users")
        except Exception as exc:
            log.warning("list_users: /Users failed: %s", exc)
            return []
        out: List[UserSpec] = []
        for u in payload or []:
            policy = (u.get("Policy") or {})
            is_admin = bool(policy.get("IsAdministrator"))
            out.append(UserSpec(
                backend_user_id=str(u.get("Id") or ""),
                username=str(u.get("Name") or ""),
                display_name=str(u.get("Name") or ""),
                role="owner" if is_admin else "managed",
                is_admin=is_admin,
            ))
        return out

    # ── Item resolution by GUID (restore-side cross-server matching) ──

    def resolve_by_guids(
        self,
        guids,
        *,
        library_id=None,
    ):
        """Look up the destination's ``backend_item_id`` for a source
        item identified by normalized GUIDs.

        Jellyfin / Emby's ``/Users/{id}/Items`` endpoint supports
        per-provider-id filtering via the ``Imdb`` / ``Tmdb`` / ``Tvdb``
        / ``MusicBrainzAlbum`` parameters. We try each provider id in
        turn; the first one that returns exactly one item wins.

        Returns ``None`` if no unambiguous match found. Caller handles
        the "skipped: no destination match" log line + run summary
        bucket the same way the resolver does on Plex."""
        uid = self._owner_user_id
        if not uid:
            self.server_identity()
            uid = self._owner_user_id
        if not uid:
            return None
        # Reverse of _PROVIDER_TO_GUID for the lookup. Build once.
        scheme_to_param = {v: k for k, v in _PROVIDER_TO_GUID.items()}
        for raw_guid in guids or ():
            if "://" not in raw_guid:
                continue
            scheme, _, value = raw_guid.partition("://")
            param_name = scheme_to_param.get(scheme.lower())
            if not param_name:
                continue
            params = {param_name: value, "Recursive": "true", "Limit": 2}
            if library_id:
                params["ParentId"] = library_id
            try:
                payload = self._get_json(
                    f"/Users/{uid}/Items", params=params,
                )
            except Exception:
                continue
            items = (payload or {}).get("Items") or []
            if len(items) == 1:
                return str(items[0].get("Id") or "")
            # Multiple matches: ambiguous, fall through to next guid.
        return None

    # ── Item enumeration ───────────────────────────────────────────────

    def iter_items(
        self,
        library_id: str,
        *,
        include_watched_only: bool = False,
        include_rated_only: bool = False,
        user_context: Optional[UserContext] = None,
    ) -> Iterator[ItemSnapshot]:
        """Walk ``GET /Users/{userId}/Items?ParentId={library_id}&Recursive=true``
        with pagination. The user-scoping is required to get per-user
        ``UserData`` (PlayCount / PlaybackPositionTicks / IsFavorite /
        Rating) back on each item; without a user, the response carries
        no playback state."""
        uid = (user_context.backend_user_id if user_context else "") or self._owner_user_id
        if not uid:
            log.warning(
                "iter_items: no user_context and no owner_user_id; "
                "ensuring it via server_identity()"
            )
            self.server_identity()
            uid = self._owner_user_id
        if not uid:
            return  # cannot enumerate without a user
        params: Dict[str, Any] = {
            "ParentId": library_id,
            "Recursive": "true",
            "IncludeItemTypes": _LEAF_ITEM_TYPES,
            "Fields": "ProviderIds,Path,MediaSources,ProductionYear",
            "EnableUserData": "true",
        }
        # Compose Filters list. Jellyfin's filter ladder applies AND
        # semantics (an item must satisfy every listed filter), so
        # "watched + rated" returns the intersection - which matches
        # the engine's "this user has both watch state AND rating
        # state" pre-filter intent.
        filters: List[str] = []
        if include_watched_only:
            filters.append("IsPlayed")
        if include_rated_only:
            filters.append("IsFavorite")  # closest proxy; numeric Rating has no Filter
        if filters:
            params["Filters"] = ",".join(filters)

        for raw in self._paginate(
            f"/Users/{uid}/Items", params=params,
        ):
            yield _jellyfin_item_to_snapshot(raw, library_id=library_id)

    # ── Per-user watch / rating writes ─────────────────────────────────

    def set_watched(
        self,
        item_ref: ItemRef,
        *,
        view_count: int,
        last_viewed_at: Optional[float],
        user_context: UserContext,
    ) -> WriteResult:
        """``POST /Users/{userId}/PlayedItems/{itemId}?DatePlayed=<iso>``.

        Jellyfin's mark-played endpoint sets ``Played=true`` and
        increments ``PlayCount`` by 1 per call. For absolute-count
        merge strategies (engine D-RATE-equivalent) the adapter
        respects the per-call cap from
        ``services.state.VIEWCOUNT_INCREMENT_CAP`` and issues N calls.
        Unplayed (``view_count == 0``) routes through DELETE same
        endpoint."""
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            return WriteResult.fail("no user_id in context")
        try:
            if int(view_count) <= 0:
                self._delete(
                    f"/Users/{uid}/PlayedItems/{item_ref.backend_item_id}",
                )
                return WriteResult.ok("marked unplayed")
            from services import state as _state
            cap = int(getattr(_state, "VIEWCOUNT_INCREMENT_CAP", 999))
            increments = max(1, min(int(view_count), cap))
            params: Dict[str, Any] = {}
            if last_viewed_at:
                # Convert unix epoch -> ISO 8601 in UTC.
                import datetime as _dt
                params["DatePlayed"] = (
                    _dt.datetime.utcfromtimestamp(last_viewed_at)
                    .replace(microsecond=0).isoformat() + "Z"
                )
            for _ in range(increments):
                self._post_json(
                    f"/Users/{uid}/PlayedItems/{item_ref.backend_item_id}",
                    params=params,
                )
            return WriteResult.ok(f"played {increments} time(s)")
        except Exception as exc:
            return WriteResult.fail(f"set_watched failed: {exc}")

    def set_resume_position(
        self,
        item_ref: ItemRef,
        offset_ms: int,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """``POST /Users/{userId}/Items/{itemId}/UserData`` with
        ``PlaybackPositionTicks`` (Jellyfin's preferred direct-write
        path; the alternative ``/Sessions/Playing/Progress`` requires
        a real playback session)."""
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            return WriteResult.fail("no user_id in context")
        try:
            ticks = int(offset_ms) * _TICKS_PER_MS
            self._post_json(
                f"/Users/{uid}/Items/{item_ref.backend_item_id}/UserData",
                json_body={"PlaybackPositionTicks": ticks},
            )
            return WriteResult.ok()
        except Exception as exc:
            return WriteResult.fail(f"set_resume_position failed: {exc}")

    def set_rating(
        self,
        item_ref: ItemRef,
        rating: float,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """Writes the numeric ``UserData.Rating`` (0-10). Jellyfin's
        binary ``Likes`` toggle is a separate concept exposed via
        :meth:`set_favorite`; the engine's D-RATE mapping decides
        whether to write both."""
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            return WriteResult.fail("no user_id in context")
        try:
            self._post_json(
                f"/Users/{uid}/Items/{item_ref.backend_item_id}/UserData",
                json_body={"Rating": float(rating)},
            )
            return WriteResult.ok()
        except Exception as exc:
            return WriteResult.fail(f"set_rating failed: {exc}")

    def set_favorite(
        self,
        item_ref: ItemRef,
        favorite: bool,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """``POST /Users/{userId}/FavoriteItems/{itemId}`` for
        favorite=True; DELETE same endpoint for favorite=False."""
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            return WriteResult.fail("no user_id in context")
        try:
            path = f"/Users/{uid}/FavoriteItems/{item_ref.backend_item_id}"
            if favorite:
                self._post_json(path)
            else:
                self._delete(path)
            return WriteResult.ok()
        except Exception as exc:
            return WriteResult.fail(f"set_favorite failed: {exc}")

    # ── Playlists ──────────────────────────────────────────────────────

    def list_playlists(self, user_context: UserContext) -> List[PlaylistSpec]:
        """Walks ``GET /Users/{userId}/Items?IncludeItemTypes=Playlist``,
        then resolves each playlist's items via
        ``GET /Playlists/{id}/Items``."""
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            return []
        try:
            playlists_raw = list(self._paginate(
                f"/Users/{uid}/Items",
                params={
                    "IncludeItemTypes": "Playlist",
                    "Recursive": "true",
                    "Fields": "ProviderIds",
                },
            ))
        except Exception as exc:
            log.warning("list_playlists: %s", exc)
            return []
        out: List[PlaylistSpec] = []
        for p in playlists_raw:
            pid = str(p.get("Id") or "")
            if not pid:
                continue
            items_tuple: tuple = ()
            try:
                items_raw = list(self._paginate(
                    f"/Playlists/{pid}/Items",
                    params={"UserId": uid, "Fields": "ProviderIds"},
                ))
                items_tuple = tuple(
                    ItemRef(
                        backend_item_id=str(it.get("Id") or ""),
                        guids=tuple(_provider_ids_to_guids(it.get("ProviderIds") or {})),
                        title=str(it.get("Name") or ""),
                    )
                    for it in items_raw
                )
            except Exception as exc:
                log.debug("playlist %s items fetch failed: %s", pid, exc)
            out.append(PlaylistSpec(
                playlist_id=pid,
                name=str(p.get("Name") or ""),
                is_smart=False,  # Jellyfin smart playlists are plugin-mediated
                smart_filter_json=None,
                items=items_tuple,
            ))
        return out

    def get_playlist_items(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> tuple:
        """Single-playlist item fetch. Avoids the full list_playlists
        scan when the caller only needs one playlist's contents
        (Playlist Management copy path, cache refresh)."""
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid or not playlist_id:
            return ()
        try:
            items_raw = list(self._paginate(
                f"/Playlists/{playlist_id}/Items",
                params={"UserId": uid, "Fields": "ProviderIds"},
            ))
        except Exception as exc:
            log.debug("get_playlist_items %s failed: %s", playlist_id, exc)
            return ()
        return tuple(
            ItemRef(
                backend_item_id=str(it.get("Id") or ""),
                guids=tuple(_provider_ids_to_guids(it.get("ProviderIds") or {})),
                title=str(it.get("Name") or ""),
            )
            for it in items_raw
        )

    def create_playlist(
        self,
        name: str,
        items: List[ItemRef],
        *,
        user_context: UserContext,
    ) -> str:
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            raise ValueError("no user_id in context")
        ids = [ref.backend_item_id for ref in items if ref.backend_item_id]
        # Inspect the first item to determine MediaType. Items returned
        # by iter_items have ``type`` populated.
        media_type = "Video"
        for ref in items:
            t = getattr(ref, "title", "") or ""
            # ItemRef carries title + library_id; we don't know the
            # leaf type without a lookup. Default Video; engine can
            # override via the snapshot's leaf type if needed.
            _ = t
            break
        body: Dict[str, Any] = {
            "Name": name, "Ids": ids, "UserId": uid, "MediaType": media_type,
        }
        resp = self._post_json("/Playlists", json_body=body)
        # Response: {Id: "<playlist-id>"}
        return str((resp or {}).get("Id") or "")

    def add_to_playlist(
        self,
        playlist_id: str,
        items: List[ItemRef],
        *,
        user_context: UserContext,
    ) -> int:
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            return 0
        ids = [ref.backend_item_id for ref in items if ref.backend_item_id]
        if not ids:
            return 0
        # POST /Playlists/{id}/Items?Ids=...&UserId=... accepts up to
        # the URL-length cap; same chunking pattern as Plex.
        chunk = 200
        added = 0
        for i in range(0, len(ids), chunk):
            slice_ = ids[i:i + chunk]
            self._post_json(
                f"/Playlists/{playlist_id}/Items",
                params={"Ids": ",".join(slice_), "UserId": uid},
            )
            added += len(slice_)
        return added

    # ── Collections (BoxSets) ──────────────────────────────────────────

    def list_collections(
        self, library_id: Optional[str] = None,
    ) -> List[CollectionSpec]:
        """Jellyfin BoxSets live in their own implicit ``boxsets``
        library. ``library_id`` is ignored (BoxSets are server-wide)."""
        uid = self._owner_user_id
        if not uid:
            self.server_identity()
            uid = self._owner_user_id
        if not uid:
            return []
        try:
            boxsets_raw = list(self._paginate(
                f"/Users/{uid}/Items",
                params={
                    "IncludeItemTypes": "BoxSet",
                    "Recursive": "true",
                    "Fields": "ProviderIds",
                },
            ))
        except Exception as exc:
            log.warning("list_collections: %s", exc)
            return []
        out: List[CollectionSpec] = []
        for c in boxsets_raw:
            cid = str(c.get("Id") or "")
            if not cid:
                continue
            items_tuple: tuple = ()
            try:
                items_raw = list(self._paginate(
                    f"/Users/{uid}/Items",
                    params={"ParentId": cid, "Fields": "ProviderIds"},
                ))
                items_tuple = tuple(
                    ItemRef(
                        backend_item_id=str(it.get("Id") or ""),
                        guids=tuple(_provider_ids_to_guids(it.get("ProviderIds") or {})),
                        title=str(it.get("Name") or ""),
                    )
                    for it in items_raw
                )
            except Exception as exc:
                log.debug("boxset %s items fetch failed: %s", cid, exc)
            out.append(CollectionSpec(
                collection_id=cid,
                name=str(c.get("Name") or ""),
                library_id=None,  # Jellyfin BoxSets are server-wide
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
        """``POST /Collections?Name=...&Ids=...``. Returns the new
        BoxSet's Id. ``library_id`` is accepted but ignored (BoxSets
        are server-wide)."""
        ids = [ref.backend_item_id for ref in items if ref.backend_item_id]
        params = {"Name": name}
        if ids:
            params["Ids"] = ",".join(ids[:200])  # initial batch
        resp = self._post_json("/Collections", params=params)
        cid = str((resp or {}).get("Id") or "")
        # Add remaining items if we chunked the initial set.
        if cid and len(ids) > 200:
            self.add_to_collection(cid, items[200:])
        return cid

    def add_to_collection(
        self,
        collection_id: str,
        items: List[ItemRef],
    ) -> int:
        ids = [ref.backend_item_id for ref in items if ref.backend_item_id]
        if not ids:
            return 0
        chunk = 200
        added = 0
        for i in range(0, len(ids), chunk):
            slice_ = ids[i:i + chunk]
            self._post_json(
                f"/Collections/{collection_id}/Items",
                params={"Ids": ",".join(slice_)},
            )
            added += len(slice_)
        return added

    # ── User management ───────────────────────────────────────────────

    def create_user(
        self,
        username: str,
        *,
        password: str,
        is_admin: bool = False,
        policy: Optional[UserPolicy] = None,
    ) -> Optional[UserSpec]:
        """``POST /Users/New`` with ``{Name, Password}``. If ``is_admin``
        or a custom ``policy`` is supplied, follows up with
        ``POST /Users/{id}/Policy`` to apply it."""
        try:
            body = {"Name": username, "Password": password}
            resp = self._post_json("/Users/New", json_body=body)
            user_id = str((resp or {}).get("Id") or "")
            if not user_id:
                return None
            if is_admin or policy is not None:
                policy_body = _build_user_policy(policy, is_admin=is_admin)
                self._post_json(
                    f"/Users/{user_id}/Policy", json_body=policy_body,
                )
            return UserSpec(
                backend_user_id=user_id,
                username=username,
                display_name=username,
                role="owner" if is_admin else "managed",
                is_admin=is_admin,
            )
        except Exception as exc:
            log.warning("create_user(%r) failed: %s", username, exc)
            return None

    def set_user_password(
        self, backend_user_id: str, *, password: str,
    ) -> WriteResult:
        try:
            # ResetPassword + NewPw sets the new password without
            # requiring the current one (admin path).
            self._post_json(
                f"/Users/{backend_user_id}/Password",
                json_body={"NewPw": password, "ResetPassword": False},
            )
            return WriteResult.ok()
        except Exception as exc:
            return WriteResult.fail(f"set_user_password failed: {exc}")

    def delete_user(self, backend_user_id: str) -> WriteResult:
        try:
            self._delete(f"/Users/{backend_user_id}")
            return WriteResult.ok()
        except Exception as exc:
            return WriteResult.fail(f"delete_user failed: {exc}")


# ── Module helpers ──────────────────────────────────────────────────────────

def _jellyfin_item_to_snapshot(
    raw: Dict[str, Any], *, library_id: str,
) -> ItemSnapshot:
    """Convert a raw Jellyfin item dict (as returned by
    ``GET /Users/{id}/Items``) to an ``ItemSnapshot``. Field map:

    | Jellyfin                  | ItemSnapshot           |
    |---------------------------|------------------------|
    | Id                        | backend_item_id        |
    | ProviderIds               | guids (normalized)     |
    | Name                      | title                  |
    | Type (Movie/Episode/...)  | type (lower-case)      |
    | ProductionYear            | year                   |
    | Path                      | file_path              |
    | UserData.PlayCount        | view_count             |
    | UserData.LastPlayedDate   | last_viewed_at (epoch) |
    | UserData.PlaybackPositionTicks | view_offset_ms (/10k) |
    | UserData.Rating           | user_rating            |
    | UserData.IsFavorite       | is_favorite            |
    """
    user_data = raw.get("UserData") or {}
    type_raw = str(raw.get("Type") or "")
    # Map Jellyfin's PascalCase types to our lowercase convention so the
    # engine's per-type branches (movie/episode/track) work uniformly.
    type_lower = {
        "Movie": "movie",
        "Episode": "episode",
        "Audio": "track",
        "Series": "show",
        "Season": "season",
        "MusicAlbum": "album",
        "MusicArtist": "artist",
    }.get(type_raw, type_raw.lower())

    ticks = int(user_data.get("PlaybackPositionTicks") or 0)
    offset_ms = ticks // _TICKS_PER_MS if ticks > 0 else 0

    last_played: Optional[float] = None
    lpd = user_data.get("LastPlayedDate")
    if lpd:
        try:
            import datetime as _dt
            # Jellyfin emits ISO 8601 with 'Z' suffix on UTC datetimes.
            parsed = _dt.datetime.fromisoformat(str(lpd).replace("Z", "+00:00"))
            last_played = parsed.timestamp()
        except Exception:
            last_played = None

    rating_raw = user_data.get("Rating")
    user_rating: Optional[float] = None
    if rating_raw is not None:
        try:
            user_rating = float(rating_raw)
        except (TypeError, ValueError):
            user_rating = None

    # DateCreated -> added_at (engine compatibility).
    added_at: Optional[float] = None
    date_created = raw.get("DateCreated")
    if date_created:
        try:
            import datetime as _dt
            parsed = _dt.datetime.fromisoformat(
                str(date_created).replace("Z", "+00:00"),
            )
            added_at = parsed.timestamp()
        except Exception:
            added_at = None

    # Hierarchy fields. Jellyfin Episode items carry SeriesName +
    # SeasonName; Audio items carry AlbumArtist + Album. Empty for
    # standalone items.
    show_title = ""
    season_title = ""
    artist = ""
    album = ""
    if type_lower == "episode":
        show_title = str(raw.get("SeriesName") or "")
        season_title = str(raw.get("SeasonName") or "")
    elif type_lower == "track":
        artist = str(raw.get("AlbumArtist") or raw.get("Artists", [""])[0] if raw.get("Artists") else "")
        album = str(raw.get("Album") or "")

    return ItemSnapshot(
        backend_item_id=str(raw.get("Id") or ""),
        guids=tuple(_provider_ids_to_guids(raw.get("ProviderIds") or {})),
        library_id=library_id,
        title=str(raw.get("Name") or ""),
        type=type_lower,
        year=raw.get("ProductionYear") or None,
        file_path=raw.get("Path") or None,
        view_count=int(user_data.get("PlayCount") or 0),
        last_viewed_at=last_played,
        view_offset_ms=offset_ms,
        user_rating=user_rating,
        is_favorite=bool(user_data.get("IsFavorite")),
        added_at=added_at,
        show_title=show_title,
        season_title=season_title,
        artist=artist,
        album=album,
    )


def _provider_ids_to_guids(provider_ids: Dict[str, Any]) -> List[str]:
    """Convert Jellyfin's ``ProviderIds`` dict (e.g.
    ``{"Imdb": "tt1234567", "Tmdb": "55555"}``) into the normalized
    cross-backend GUID list (``["imdb://tt1234567", "tmdb://55555"]``).
    Order is stable for matcher determinism; unknown provider keys
    pass through lowercased as ``provider://value`` so the engine's
    Tier-0 DB lookup still has something to key on."""
    raw: List[str] = []
    for k, v in (provider_ids or {}).items():
        if not v:
            continue
        scheme = _PROVIDER_TO_GUID.get(k)
        if scheme:
            raw.append(f"{scheme}://{v}")
        else:
            # Pass through unknown providers (e.g. AniDB, MyAnimeList)
            # so future translator extensions can adopt them without
            # losing data captured today.
            raw.append(f"{k.lower()}://{v}")
    return list(normalize_guids(raw))


def _normalise_collection_type(raw: Any) -> str:
    """Map Jellyfin ``CollectionType`` strings to our ``LibrarySpec.type``
    convention. Jellyfin uses lowercase plurals (movies/tvshows/music)
    while our convention is singular (movie/show/artist) to match Plex
    ``section.type`` semantics.

    Unknown CollectionType values pass through as-is so the engine can
    log the raw label rather than silently drop a library."""
    s = str(raw or "").lower()
    return {
        "movies": "movie",
        "tvshows": "show",
        "music": "artist",
        "musicvideos": "track",
        "audiobooks": "audiobook",
        "books": "book",
        "photos": "photo",
        "homevideos": "movie",
        "boxsets": "boxsets",
        "playlists": "playlists",
        "livetv": "livetv",
        "mixed": "mixed",
        "": "mixed",
    }.get(s, s)


def _build_user_policy(
    policy: Optional[UserPolicy], *, is_admin: bool,
) -> Dict[str, Any]:
    """Translate :class:`UserPolicy` (backend-agnostic) into Jellyfin's
    ``POST /Users/{id}/Policy`` body shape."""
    p = policy or UserPolicy(is_administrator=is_admin)
    return {
        "IsAdministrator": bool(p.is_administrator or is_admin),
        "IsDisabled": bool(p.is_disabled),
        "EnableAllFolders": bool(p.enable_all_folders),
        "EnabledFolders": list(p.enabled_folder_ids),
        # Sensible defaults for fields we don't currently surface in
        # UserPolicy. Jellyfin requires these on the body even if the
        # end user didn't set them.
        "EnableMediaPlayback": True,
        "EnableLiveTvAccess": False,
        "MaxActiveSessions": 0,
    }


def authenticate_by_name(
    base_url: str, *, username: str, password: str,
    device_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Top-level helper used by the end user-facing "test connection"
    path and the future server-add flow. Posts to
    ``/Users/AuthenticateByName``; returns the parsed
    ``AuthenticationResult`` (with ``AccessToken``, ``User``,
    ``ServerId`` fields). Raises ``requests.HTTPError`` on 401.

    Builds its own one-off session - no global state - because this
    runs BEFORE a JellyfinAdapter is constructed (we don't have an
    admin token yet)."""
    import uuid as _uuid
    creds = AuthCredentials(
        backend="jellyfin",
        token="",  # not yet issued
        device_id=device_id or str(_uuid.uuid4()),
    )
    # Authentication endpoint accepts the header even without a token;
    # the server issues the token in response.
    session = make_session(creds)
    resp = session.post(
        f"{base_url.rstrip('/')}/Users/AuthenticateByName",
        json={"Username": username, "Pw": password},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


__all__ = ["JellyfinAdapter", "authenticate_by_name"]
