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
from ._mirror_resolve import MirrorResolveMixin


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
# today. The count-helper ``_count_library_items`` IS
# library_type-aware and uses this map directly.
_LEAF_TYPES_BY_LIBRARY_TYPE = {
    "movie": "Movie",
    "show": "Episode",
    "artist": "Audio",
    "musicvideo": "MusicVideo",
    "audiobook": "AudioBook,Book",
    "book": "Book",
    "boxsets": "BoxSet",
    "photo": "Photo",
}


# CollectionType values that are
# aggregate VIEWS, not real item libraries. The "Collections"
# (boxsets) virtual library re-presents items that already live in
# the real Movies / TV / Music libraries; "playlists" and "livetv"
# are likewise not a source of leaf items. The server-mirror walk
# skips these so it never re-fetches items already captured under
# their true source library. Collection STRUCTURE is captured
# separately via list_collections (the IncludeItemTypes=BoxSet query).
_NON_ITEM_LIBRARY_TYPES = frozenset({"boxsets", "playlists", "livetv"})


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


# Jellyfin / Emby ``Type`` (PascalCase) -> the engine's neutral
# lowercase item-type convention (matches Plex ``section.type`` /
# item ``type``). Unmapped values still fall through lowercased in
# ``_neutral_item_type`` as a safety net, but ADAPT-07: every leaf
# type that ``_LEAF_TYPES_BY_LIBRARY_TYPE`` above can yield is now
# listed here explicitly so the two tables stay in step instead of
# relying on the implicit lowercasing to paper over gaps. The two
# tables are different namespaces and are NOT interchangeable: the
# one above maps a neutral *library* type to a Jellyfin leaf type;
# this one maps a Jellyfin *item* type to a neutral *item* type.
_JELLYFIN_TYPE_TO_NEUTRAL = {
    "Movie": "movie",
    "Episode": "episode",
    "Audio": "track",
    "Series": "show",
    "Season": "season",
    "MusicAlbum": "album",
    "MusicArtist": "artist",
    # Leaf types reachable via _LEAF_TYPES_BY_LIBRARY_TYPE - listed
    # explicitly (each equals its prior lowercasing fallthrough, so
    # this is a consistency fix with no behaviour change).
    "MusicVideo": "musicvideo",
    "AudioBook": "audiobook",
    "Book": "book",
    "Photo": "photo",
}


def _neutral_item_type(raw_type: Any) -> str:
    """Map a Jellyfin/Emby ``Type`` to the engine's lowercase
    convention. Unknown types pass through lowercased so a mixed
    library never silently drops an item."""
    t = str(raw_type or "")
    return _JELLYFIN_TYPE_TO_NEUTRAL.get(t, t.lower())


def _iso_to_epoch(value: Any) -> Optional[float]:
    """Parse a Jellyfin/Emby ISO 8601 timestamp (``...Z`` UTC suffix)
    to a unix epoch float. None on empty input or any parse failure."""
    if not value:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        return None


def _epoch_to_iso(ts: float) -> str:
    """Format a unix epoch as a UTC ISO 8601 string for Jellyfin/Emby
    query params (e.g. ``MinDateLastSaved`` for delta sync)."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(
        float(ts), tz=_dt.timezone.utc,
    ).isoformat()


def _int_or_none(value: Any) -> Optional[int]:
    """Coerce a value to int, or None when absent / non-numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class JellyfinAdapter(MirrorResolveMixin, HttpMediaAdapterMixin, MediaServerAdapter):
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
        server_uid: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        # The admin's own Jellyfin user-id, used as the implicit
        # UserContext for ``iter_items(user_context=None)`` calls and
        # as the default for playlist / collection write paths that
        # need a UserId in the URL.
        self._owner_user_id = (owner_user_id or "").strip()
        self._machine_id_cached = (machine_id or "").strip()
        # The app registry UID (server_registry.make_server_id(),
        # '<service>_<uuid4>').
        # This is the key the server mirror is rowed by - the same id
        # the sync endpoint uses - so MirrorResolveMixin can consult
        # the mirror under a backend-neutral, log-safe identifier.
        self._server_uid = (server_uid or "").strip()
        self._creds = AuthCredentials(
            backend=self._auth_scheme,
            token=admin_token,
            # Jellyfin doesn't carry UserId in the header (Emby does).
            user_id=None,
        )
        self._session = make_session(self._creds)
        # Cached on first use to keep ``server_identity`` cheap.
        self._identity_cache: Optional[ServerIdentity] = None

    def _mirror_server_id(self) -> str:
        """The app registry UID this server's mirror rows are keyed by
        (MirrorResolveMixin contract). Empty when the adapter was
        constructed outside the registry (tests / tooling), which
        leaves every mirror-first path inert and falls through to the
        live API."""
        return self._server_uid or ""

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
        include_types = _LEAF_TYPES_BY_LIBRARY_TYPE.get(
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
        item_type_hint="",
    ):
        """Look up the destination's ``backend_item_id`` for a source
        item identified by normalized GUIDs.

        ``item_type_hint`` is accepted to satisfy the ``MediaServerAdapter``
        ABC contract (the playlist-copy resolver always passes it). The
        Jellyfin/Emby ProviderIds search is already type-agnostic, so the
        hint is currently advisory only.

        Jellyfin / Emby's ``/Users/{id}/Items`` endpoint supports
        per-provider-id filtering via the ``Imdb`` / ``Tmdb`` / ``Tvdb``
        / ``MusicBrainzAlbum`` parameters. We try each provider id in
        turn; the first one that returns exactly one item wins.

        Returns ``None`` if no unambiguous match found. Caller handles
        the "skipped: no destination match" log line + run summary
        bucket the same way the resolver does on Plex.

        The server mirror
        is consulted first (MirrorResolveMixin). On a hit it answers
        from local SQLite with no API call; on a miss or always-live
        mode it falls through to the live ProviderIds search below."""
        guid_list = [g for g in (guids or ()) if g]
        self._ensure_mirror_warmed_for(library_id=library_id)
        mirror_hit = self._mirror_lookup_guids(guid_list, library_id)
        if mirror_hit:
            return mirror_hit

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

    def resolve_by_hierarchy(
        self,
        *,
        item_type: str,
        title: str,
        show_title: str = "",
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        artist: str = "",
        album: str = "",
        library_id: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve a destination ``backend_item_id`` by item
        hierarchy when the
        GUID match misses.

        Jellyfin/Emby's ``/Users/{id}/Items`` endpoint has no direct
        "filter by season + episode number" parameter, so the
        strategy is search-by-title-then-filter-in-Python:

          * Episodes: search ``IncludeItemTypes=Episode`` by the
            episode title, keep rows whose ``SeriesName`` +
            ``ParentIndexNumber`` (season) + ``IndexNumber``
            (episode) all match. season+episode is the
            disambiguator for the classic "Pilot in S1 of every
            show" case.
          * Tracks: search ``IncludeItemTypes=Audio`` by the track
            title, keep rows whose ``AlbumArtist`` (or first
            ``Artists`` entry) matches; ``Album`` is the
            tiebreaker.

        Returns the matched item id only when the filtered result
        is UNIQUE; an ambiguous set returns None so the caller can
        log + skip rather than guess. One API call per resolve.

        The server mirror
        is consulted first (MirrorResolveMixin) - its hierarchy index
        carries the parent GUID + show/artist + leaf coordinates, so a
        mirror hit answers from local SQLite with no API call. On a
        miss it falls through to the live search below.
        """
        if item_type not in ("episode", "track") or not title:
            return None

        self._ensure_mirror_warmed_for(
            item_type_hint=item_type, library_id=library_id,
        )
        mirror_hit = self._mirror_lookup_hierarchy(
            item_type=item_type, title=title,
            show_title=show_title or None,
            season_number=season_number,
            episode_number=episode_number,
            artist=artist or None,
            album=album or None,
        )
        if mirror_hit:
            return mirror_hit

        uid = self._owner_user_id
        if not uid:
            self.server_identity()
            uid = self._owner_user_id
        if not uid:
            return None
        params: Dict[str, Any] = {
            "SearchTerm": title,
            "Recursive": "true",
            "Limit": 50,
            "Fields": "ProviderIds",
        }
        if library_id:
            params["ParentId"] = library_id
        if item_type == "episode":
            params["IncludeItemTypes"] = "Episode"
        else:
            params["IncludeItemTypes"] = "Audio"
        try:
            payload = self._get_json(f"/Users/{uid}/Items", params=params)
        except Exception:
            return None
        rows = (payload or {}).get("Items") or []
        title_lc = title.strip().lower()
        matches: List[str] = []
        for r in rows:
            if str(r.get("Name") or "").strip().lower() != title_lc:
                continue
            if item_type == "episode":
                if show_title and str(r.get("SeriesName") or "").strip().lower() \
                        != show_title.strip().lower():
                    continue
                if season_number is not None:
                    _pi = r.get("ParentIndexNumber")
                    if _pi is None or int(_pi) != int(season_number):
                        continue
                if episode_number is not None:
                    _in = r.get("IndexNumber")
                    if _in is None or int(_in) != int(episode_number):
                        continue
            else:  # track
                if artist:
                    _aa = str(r.get("AlbumArtist") or "")
                    if not _aa and r.get("Artists"):
                        _aa = str((r.get("Artists") or [""])[0])
                    if _aa.strip().lower() != artist.strip().lower():
                        continue
                if album and str(r.get("Album") or "").strip().lower() \
                        != album.strip().lower():
                    continue
            iid = str(r.get("Id") or "")
            if iid:
                matches.append(iid)
        # Unique match only — an ambiguous result is no result.
        uniq = sorted(set(matches))
        return uniq[0] if len(uniq) == 1 else None

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
        # ``Filters=IsPlayed`` is applied only for the
        # ``include_watched_only`` path. It depends on correct
        # per-user auth (X-Emby-Token alone, no Authorization header,
        # per Emby's doc-sanctioned shape; see
        # dev.emby.media/doc/restapi/User-Authentication.html):
        # admin-impersonation gives the wrong UserData scope and every
        # item comes back PlayCount=0 / Played=False regardless of
        # filter. Without the filter the bulk walk pulls every item in
        # the library just to drop most of it client-side; with it,
        # Emby returns only the played subset.
        #
        # The ``IsFavorite`` proxy is NOT used for
        # ``include_rated_only`` because a numeric Rating is not the
        # same as a binary IsFavorite.
        #
        # Per-call decision: only apply when
        # ``include_watched_only=True``. The ratings path needs
        # numeric ``UserData.Rating`` which is independent of
        # ``IsPlayed`` and would be wrong to narrow by it.
        if include_watched_only:
            params["Filters"] = "IsPlayed"
        extra_headers: Optional[Dict[str, Optional[str]]] = None
        uses_per_user_token = False
        if user_context is not None and self.backend in ("emby", "jellyfin"):
            target_uid = user_context.backend_user_id or uid
            user_token = user_context.auth_token or self._admin_token
            uses_per_user_token = (
                user_token and user_token != self._admin_token
            )
            # Per dev.emby.media/doc/restapi/User-Authentication.html,
            # post-login calls should use ``X-Emby-Token`` ONLY - the
            # full ``Authorization: Emby ...`` header is for the
            # AuthenticateByName login call. Sending BOTH on a
            # per-user request is not doc-sanctioned, and the
            # duplicate Authorization header can trigger a different
            # identity-resolution path in Emby where per-user UserData
            # comes back as zeros even though the token is correct.
            #
            # So: set X-Emby-Token to the right token (per-user when
            # we have one, admin otherwise) and STRIP the
            # session-default Authorization header for this request by
            # passing ``Authorization: None`` so requests removes it
            # from the per-request map.
            if uses_per_user_token:
                extra_headers = {
                    "X-Emby-Token": user_token,
                    "Authorization": None,  # type: ignore[dict-item]
                }
            elif self.backend == "emby":
                # No per-user token stored - admin-impersonation
                # fallback. Use admin's X-Emby-Token; the URL path
                # /Users/{target_uid}/Items conveys the target
                # user. Same Authorization-strip rule applies.
                extra_headers = {
                    "X-Emby-Token": user_token,
                    "Authorization": None,  # type: ignore[dict-item]
                }
            # NOTE: previously we also set ``params["UserId"] =
            # target_uid`` as a belt-and-braces query param. Emby's
            # documented param set for /Users/{uid}/Items does NOT
            # include UserId (the URL path already conveys it).
            # The redundant param has been removed.

        for raw in self._paginate(
            f"/Users/{uid}/Items", params=params,
            extra_headers=extra_headers,
        ):
            yield _jellyfin_item_to_snapshot(raw, library_id=library_id)

    # ── Server mirror provider ───────────────────────────────────────
    #
    # JellyfinAdapter - and EmbyAdapter, which inherits these two
    # methods unchanged (it differs only in auth headers, handled by
    # _get_json / _paginate) - feeds the per-server metadata mirror
    # exactly as PlexAdapter does. iter_sections_for_mirror enumerates
    # libraries; the sync layer then drives iter_section_items_for_mirror
    # per section. The mirror caches the item universe so cross-server
    # resolution answers from SQLite instead of a live API walk.

    def iter_sections_for_mirror(self) -> List[Any]:
        """Yield ``services.server_mirror.SectionInfo`` for every
        library section.

        ``live_total_size`` is the leaf item count (one cheap count
        query per library); ``live_updated_at`` is the newest item's
        DateLastSaved (one cheap top-1 query). Together they give the
        sync layer's freshness probe the same full-vs-delta-vs-probe
        signal PlexAdapter derives from ``section.totalSize`` +
        ``section.updatedAt``."""
        from services.server_mirror import SectionInfo
        out: List[Any] = []
        try:
            payload = self._get_json("/Library/VirtualFolders")
        except Exception as exc:
            log.warning(
                "iter_sections_for_mirror: VirtualFolders failed: %s", exc,
            )
            return out
        if not self._owner_user_id:
            try:
                self.server_identity()
            except Exception:
                pass
        uid = self._owner_user_id
        for entry in payload or []:
            lib_id = str(entry.get("ItemId") or entry.get("Id") or "")
            if not lib_id:
                continue
            lib_type = _normalise_collection_type(entry.get("CollectionType"))
            if lib_type in _NON_ITEM_LIBRARY_TYPES:
                # Aggregate view (Collections / Playlists / Live TV),
                # not a real item library. Skipping it keeps the mirror
                # walk from re-fetching items already captured under
                # their real source library. Collection membership is
                # captured separately via list_collections.
                continue
            total: Optional[int] = None
            updated: Optional[float] = None
            if uid:
                total = self._count_library_items(uid, lib_id, lib_type)
                updated = self._newest_item_saved_at(uid, lib_id)
            out.append(SectionInfo(
                section_id=lib_id,
                name=str(entry.get("Name") or ""),
                section_type=lib_type,
                live_total_size=total,
                live_updated_at=updated,
            ))
        return out

    def _newest_item_saved_at(
        self, uid: str, library_id: str,
    ) -> Optional[float]:
        """Newest leaf-item DateLastSaved in a library, as a unix
        epoch. An item edited without changing the library's count
        still shifts this, so it is a real freshness signal. None
        when the library is empty or the server does not honor the
        sort - the probe then degrades to count-only drift."""
        try:
            data = self._get_json(
                f"/Users/{uid}/Items",
                params={
                    "ParentId": library_id,
                    "Recursive": "true",
                    "IncludeItemTypes": _LEAF_ITEM_TYPES,
                    "SortBy": "DateLastSaved",
                    "SortOrder": "Descending",
                    "Limit": 1,
                    "Fields": "DateLastSaved",
                    "EnableUserData": "false",
                },
            )
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        items = data.get("Items") or []
        if not items:
            return None
        return _iso_to_epoch(items[0].get("DateLastSaved"))

    def _build_parent_guid_map(
        self, uid: str, section_id: str, parent_type: str,
    ) -> Dict[str, str]:
        """Map a section's parent-container items - ``Series`` for a
        show library, ``MusicArtist`` for a music library - to their
        first normalized cross-server GUID.

        Jellyfin / Emby leaf items carry only the parent's INTERNAL id
        (``SeriesId`` / album-artist id), which differs per server and
        is useless as a cross-server key. Walking the (far smaller)
        parent set once per section and reading each parent's
        ProviderIds yields a real ``grandparent_guid`` for the leaf
        rows - the strongest key for the resolver's hierarchy tier."""
        out: Dict[str, str] = {}
        try:
            for raw in self._paginate(
                f"/Users/{uid}/Items",
                params={
                    "ParentId": section_id,
                    "Recursive": "true",
                    "IncludeItemTypes": parent_type,
                    "Fields": "ProviderIds",
                    "EnableUserData": "false",
                },
            ):
                pid = str(raw.get("Id") or "")
                if not pid:
                    continue
                guids = _provider_ids_to_guids(raw.get("ProviderIds") or {})
                if guids:
                    out[pid] = guids[0]
        except Exception as exc:
            log.debug(
                "parent-guid map (%s) failed for section %s: %s",
                parent_type, section_id, exc,
            )
        return out

    def iter_section_items_for_mirror(
        self, section_id: str, since_ts: Optional[float] = None,
    ) -> Iterator[Any]:
        """Yield ``services.server_mirror.ItemRow`` for every leaf item
        in a library section. The sync layer's ``item_provider``.

        Delta sync: when ``since_ts`` is set the walk asks the server
        for only items saved since then (``MinDateLastSaved``) AND
        filters client-side on ``DateLastSaved``, so a server that
        ignores the param still produces a correct (just slower)
        delta.

        ``grandparent_guid`` is backfilled lazily: the parent-container
        GUID map for a type is built only on first encounter of a leaf
        of that type, so a movie library builds neither map.

        ``EnableUserData=false`` - the mirror stores the item universe,
        not per-user playback state."""
        from services.server_mirror import ItemRow
        uid = self._owner_user_id
        if not uid:
            try:
                self.server_identity()
            except Exception:
                pass
            uid = self._owner_user_id
        if not uid:
            return

        params: Dict[str, Any] = {
            "ParentId": section_id,
            "Recursive": "true",
            "IncludeItemTypes": _LEAF_ITEM_TYPES,
            "Fields": "ProviderIds,Path,DateLastSaved,AlbumArtists",
            "EnableUserData": "false",
        }
        if since_ts is not None:
            params["MinDateLastSaved"] = _epoch_to_iso(since_ts)

        # Parent-GUID maps, built lazily on first leaf of each type.
        series_guid: Optional[Dict[str, str]] = None
        artist_guid: Optional[Dict[str, str]] = None

        for raw in self._paginate(f"/Users/{uid}/Items", params=params):
            rk = str(raw.get("Id") or "")
            if not rk:
                continue
            saved_at = _iso_to_epoch(raw.get("DateLastSaved"))
            # Client-side delta guard for a server that does not honor
            # MinDateLastSaved.
            if (since_ts is not None and saved_at is not None
                    and saved_at <= since_ts):
                continue
            item_type = _neutral_item_type(raw.get("Type"))

            gpg: Optional[str] = None
            artist_name: Optional[str] = None
            album_name: Optional[str] = None
            show_title: Optional[str] = None
            season_no: Optional[int] = None
            episode_no: Optional[int] = None
            parent_rk: Optional[str] = None

            if item_type == "episode":
                show_title = str(raw.get("SeriesName") or "") or None
                season_no = _int_or_none(raw.get("ParentIndexNumber"))
                episode_no = _int_or_none(raw.get("IndexNumber"))
                parent_rk = str(raw.get("SeriesId") or "") or None
                if series_guid is None:
                    series_guid = self._build_parent_guid_map(
                        uid, section_id, "Series",
                    )
                if parent_rk:
                    gpg = series_guid.get(parent_rk)
            elif item_type == "track":
                album_name = str(raw.get("Album") or "") or None
                _aa = raw.get("AlbumArtists") or []
                _aa0 = _aa[0] if _aa and isinstance(_aa[0], dict) else {}
                artist_name = str(_aa0.get("Name") or "") or None
                if not artist_name:
                    artist_name = str(raw.get("AlbumArtist") or "") or None
                _aa_id = str(_aa0.get("Id") or "")
                parent_rk = _aa_id or (str(raw.get("AlbumId") or "") or None)
                if artist_guid is None:
                    artist_guid = self._build_parent_guid_map(
                        uid, section_id, "MusicArtist",
                    )
                if _aa_id:
                    gpg = artist_guid.get(_aa_id)

            yield ItemRow(
                rating_key=rk,
                title=str(raw.get("Name") or ""),
                item_type=item_type,
                file_path=raw.get("Path") or None,
                guids=tuple(
                    _provider_ids_to_guids(raw.get("ProviderIds") or {})
                ),
                artist=artist_name,
                album=album_name,
                show_title=show_title,
                season_number=season_no,
                episode_number=episode_no,
                parent_rating_key=parent_rk,
                grandparent_guid=gpg,
                live_updated_at=saved_at,
            )

    # ── Per-user watch / rating writes ─────────────────────────────────

    def set_watched(
        self,
        item_ref: ItemRef,
        *,
        view_count: int,
        last_viewed_at: Optional[float],
        user_context: UserContext,
        current_view_count: Optional[int] = None,
    ) -> WriteResult:
        """``POST /Users/{userId}/Items/{itemId}/UserData`` with
        ``{"PlayCount": N, "Played": <bool>, "LastPlayedDate": <iso>}``.

        Jellyfin + Emby both expose a direct UserData endpoint that
        sets EXACT play counts in a single call. This is used in
        preference to looping ``POST /Users/{uid}/PlayedItems/{itemId}``
        (which increments ``PlayCount`` by 1 per call): the increment
        loop can never set an absolute target lower than the current
        count, and it issues N round-trips per item for N watched
        plays.

        With the UserData endpoint:
          * ``view_count >= 1``: ``PlayCount=N`` + ``Played=true`` +
            optional ``LastPlayedDate`` (ISO 8601 UTC).
          * ``view_count == 0``: ``PlayCount=0`` + ``Played=false``
            (one call also clears the "watched" flag — no separate
            DELETE needed).

        Single call sets the destination's row to the exact target
        regardless of prior state, so the watch-sync + snapshot
        restore paths can write the exact count they computed
        (or captured) without any delta-loop logic.
        """
        uid = user_context.backend_user_id or self._owner_user_id
        if not uid:
            return WriteResult.fail("no user_id in context")
        try:
            n = max(0, int(view_count))
            body: Dict[str, Any] = {
                "PlayCount": n,
                "Played": n > 0,
            }
            if last_viewed_at and n > 0:
                # Convert unix epoch -> ISO 8601 in UTC. The
                # destination uses this as the LastPlayedDate so the
                # restored row reads "last watched <date>" identically
                # to the snapshot source.
                import datetime as _dt
                body["LastPlayedDate"] = (
                    _dt.datetime.utcfromtimestamp(last_viewed_at)
                    .replace(microsecond=0).isoformat() + "Z"
                )
            self._post_json(
                f"/Users/{uid}/Items/{item_ref.backend_item_id}/UserData",
                json_body=body,
            )
            return WriteResult.ok(
                f"set PlayCount={n}, Played={bool(n > 0)}"
            )
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
                    "Fields": "ProviderIds,MediaType",
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
                    params={
                        "UserId": uid,
                        "Fields": "ProviderIds,ParentId",
                    },
                ))
                items_tuple = tuple(
                    ItemRef(
                        backend_item_id=str(it.get("Id") or ""),
                        guids=tuple(_provider_ids_to_guids(it.get("ProviderIds") or {})),
                        title=str(it.get("Name") or ""),
                        # Include ParentId so the adapter restorer's
                        # Replace-mode scoping can attribute each item
                        # to its source library.
                        library_id=str(it.get("ParentId") or "") or None,
                    )
                    for it in items_raw
                )
            except Exception as exc:
                log.debug("playlist %s items fetch failed: %s", pid, exc)
            # Surface the playlist's MediaType so the
            # Playlist Mgmt UI can group video / audio / photo
            # playlists separately. Jellyfin / Emby return
            # ``MediaType`` as "Audio" / "Video" / "Photo" / "" on
            # the lightweight list response (no extra HTTP).
            mtype = str(p.get("MediaType") or "").strip().lower()
            playlist_type = mtype if mtype in (
                "audio", "video", "photo",
            ) else ""
            out.append(PlaylistSpec(
                playlist_id=pid,
                name=str(p.get("Name") or ""),
                is_smart=False,  # Jellyfin smart playlists are plugin-mediated
                smart_filter_json=None,
                items=items_tuple,
                playlist_type=playlist_type,
                # Jellyfin / Emby playlists don't expose a primary
                # library on the lightweight response. Items DO
                # carry ParentId now; the UI can re-derive the
                # majority parent if it wants. Leaving these None
                # here keeps the adapter response small.
                primary_library_id=None,
                primary_library_name=None,
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
        # Derive the playlist MediaType from the items. Jellyfin types
        # a playlist Audio / Video / Photo; a music playlist created as
        # "Video" is mis-typed and the client filters it out of the
        # music view. Use the first item carrying a known neutral type.
        _MEDIA_TYPE_FOR_ITEM = {
            "track": "Audio", "album": "Audio", "artist": "Audio",
            "movie": "Video", "episode": "Video", "show": "Video",
            "season": "Video", "musicvideo": "Video",
            "photo": "Photo",
        }
        media_type = ""
        for ref in items:
            nt = (getattr(ref, "item_type", "") or "").strip().lower()
            mt = _MEDIA_TYPE_FOR_ITEM.get(nt)
            if mt:
                media_type = mt
                break
        # Fall back to Audio when no item carries a type: playlist copy
        # in this app is predominantly music, and Jellyfin's music view
        # is the surface that hard-filters on MediaType.
        if not media_type:
            media_type = "Audio"
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

    # ── Replace-mode deletions ─────────────────────────────────────────

    def delete_playlist(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """Jellyfin / Emby store playlists as items, deletable via the
        generic ``DELETE /Items/{id}`` route. The admin token has
        delete authority across all users' playlists."""
        if not playlist_id:
            return WriteResult.fail("empty playlist_id")
        try:
            self._delete(f"/Items/{playlist_id}")
        except Exception as exc:
            return WriteResult.fail(
                f"playlist {playlist_id!r} delete failed: {exc}"
            )
        return WriteResult.ok()

    def delete_collection(
        self,
        collection_id: str,
    ) -> WriteResult:
        """BoxSets are also items in the Jellyfin / Emby model so the
        same ``DELETE /Items/{id}`` route applies."""
        if not collection_id:
            return WriteResult.fail("empty collection_id")
        try:
            self._delete(f"/Items/{collection_id}")
        except Exception as exc:
            return WriteResult.fail(
                f"collection {collection_id!r} delete failed: {exc}"
            )
        return WriteResult.ok()

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
    type_lower = _neutral_item_type(type_raw)

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
    # SeasonName + ParentIndexNumber (season number) + IndexNumber
    # (episode number); Audio items carry AlbumArtist + Album. Empty
    # for standalone items.
    show_title = ""
    season_title = ""
    season_index: Optional[int] = None
    episode_index: Optional[int] = None
    artist = ""
    album = ""
    if type_lower == "episode":
        show_title = str(raw.get("SeriesName") or "")
        season_title = str(raw.get("SeasonName") or "")
        # ParentIndexNumber + IndexNumber are integers when set, None
        # for episodes lacking metadata. Coerce defensively so a
        # string "1" surfaces as int(1) and an unexpected type
        # short-circuits to None rather than crashing the parser.
        _pin = raw.get("ParentIndexNumber")
        if _pin is not None:
            try:
                season_index = int(_pin)
            except (TypeError, ValueError):
                season_index = None
        _in = raw.get("IndexNumber")
        if _in is not None:
            try:
                episode_index = int(_in)
            except (TypeError, ValueError):
                episode_index = None
    elif type_lower == "track":
        artist = str(raw.get("AlbumArtist") or (raw.get("Artists") or [""])[0])
        album = str(raw.get("Album") or "")

    # grandparent_guid is intentionally NOT captured on the
    # Jellyfin/Emby path. The episode/track item carries only
    # ``SeriesId`` / ``AlbumId`` - the parent's INTERNAL Jellyfin
    # item id, which differs per server and is useless as a
    # cross-server key. A real parent GUID would need a separate
    # fetch of the series/artist item's ProviderIds. The resolver's
    # hierarchy tier falls back to the show_title/season/episode +
    # artist/album title-and-index path for J/E. Plex, which exposes
    # grandparentGuid for free, gets the GUID path.

    # Emby returns items with ``Played=True`` and ``PlayCount=0`` for
    # users who marked the item watched without finishing a real
    # playback session ("Mark as watched" in the web UI, or
    # scrobble-style flags from other clients). Reading PlayCount
    # alone misses these rows entirely. Coerce to ``max(PlayCount,
    # 1)`` when Played is truthy so the downstream "view_count > 0"
    # filter in snapshotter_adapter._capture_watch_history honors them.
    _pc_raw = user_data.get("PlayCount")
    try:
        _pc = int(_pc_raw or 0)
    except (TypeError, ValueError):
        _pc = 0
    _played = bool(user_data.get("Played"))
    if _played and _pc < 1:
        _pc = 1
    return ItemSnapshot(
        backend_item_id=str(raw.get("Id") or ""),
        guids=tuple(_provider_ids_to_guids(raw.get("ProviderIds") or {})),
        library_id=library_id,
        title=str(raw.get("Name") or ""),
        type=type_lower,
        year=raw.get("ProductionYear") or None,
        file_path=raw.get("Path") or None,
        view_count=_pc,
        last_viewed_at=last_played,
        view_offset_ms=offset_ms,
        user_rating=user_rating,
        is_favorite=bool(user_data.get("IsFavorite")),
        added_at=added_at,
        show_title=show_title,
        season_title=season_title,
        season_index=season_index,
        episode_index=episode_index,
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
        # ADAPT-10: maps to the "musicvideo" key in _LEAF_TYPES_BY_LIBRARY_TYPE
        "musicvideos": "musicvideo",
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
    backend: str = "jellyfin",
    device_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Top-level helper used by the end user-facing "test connection"
    path and the per-user-token capture flow in
    :mod:`server.user_capture`. Posts to ``/Users/AuthenticateByName``;
    returns the parsed ``AuthenticationResult`` (with ``AccessToken``,
    ``User``, ``ServerId`` fields). Raises ``requests.HTTPError`` on
    401 / wrong credentials.

    ``backend`` selects the Authorization scheme name
    ("MediaBrowser" for Jellyfin, "Emby" for Emby). The endpoint path
    and JSON shape are identical between the two server families;
    only the header scheme differs.

    For Emby "Easy PIN" users the PIN is passed as ``password`` (Emby
    accepts a PIN-only password through the same endpoint). For
    Jellyfin users the stored password is passed verbatim.

    Builds its own one-off session - no global state - because this
    runs BEFORE an adapter is constructed (we don't have a token
    yet for a session)."""
    import uuid as _uuid
    creds = AuthCredentials(
        backend=backend,
        token="",  # not yet issued; omitted from Authorization header
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
    if not resp.ok:
        # Surface the server's response body in the raised exception.
        # A bare ``raise_for_status()`` produces "401 Client Error:
        # Unauthorized for url: ..." with no detail, leaving the
        # operator guessing whether the rejection was bad
        # credentials, header pre-flight failure,
        # PIN-restricted-to-local-network, or something else.
        # Emby/Jellyfin typically return a short text body explaining
        # the rejection.
        body_preview = ""
        try:
            body_preview = (resp.text or "")[:512]
        except Exception:
            body_preview = "<could not read body>"
        from requests import HTTPError
        raise HTTPError(
            f"{resp.status_code} {resp.reason} for "
            f"POST {resp.url} (body: {body_preview!r})",
            response=resp,
        )
    resp.raise_for_status()
    return resp.json()


__all__ = ["JellyfinAdapter", "authenticate_by_name"]
