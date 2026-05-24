"""
Backend-agnostic media server interface for Hestia-MediaManager.

The engine (services/snapshotter.py + services/restorer.py + the direct
transfer + restore wrappers in server/) historically called plexapi
methods directly: ``server.library.section(name).searchTracks()``,
``Playlist.create(server, ...)``, the ``/:/scrobble`` + ``/:/rate``
endpoints, etc. That hard-couples the engine to Plex's data shape and
URL surface.

This package replaces those direct calls with one ABC
(``MediaServerAdapter``) plus per-backend implementations
(``PlexAdapter`` today; ``JellyfinAdapter`` / ``EmbyAdapter`` in
PR-Backends). The engine receives an adapter instance and calls into
this surface area only. Backends are interchangeable from the engine's
point of view; per-backend HTTP details, identifier formats, and quirks
live inside the adapter classes.

Design principles
-----------------

1. **Derived from current call sites, not from roadmapplan4.** The ABC
   shape matches what the engine actually does today; over-abstraction
   in this PR creates churn for PR-Backends. Anything the engine doesn't
   call today is not on the ABC.

2. **Stable backend-native identifiers, opaque to the engine.** Items,
   libraries, users, playlists, and collections each have a
   ``*_id`` field that the engine treats as opaque. Plex uses string
   forms of integer ratingKeys / section keys / numeric user IDs;
   Jellyfin and Emby use GUIDs. The engine never parses these.

3. **GUIDs are the cross-backend item identity.** ``ItemRef.guids``
   carries the normalized upstream identifiers (``imdb://tt...``,
   ``tmdb://...``, ``tvdb://...``, ``musicbrainz://...``) produced by
   ``services/guid_translator.py``. Cross-backend matching uses these,
   never the backend-native ``backend_item_id``.

4. **Write results carry capability info.** ``WriteResult.unsupported``
   distinguishes "the backend can't do this" (e.g. ``set_favorite`` on
   Plex) from "the call failed." The run summary breaks these out so
   end users see "12 ratings skipped: backend does not support
   per-user favorite toggles" rather than a confusing failure count.

5. **Per-user vs admin scoping is explicit via UserContext.** Plex
   needs per-user tokens for per-user data reads. Jellyfin and Emby
   admin tokens can write on behalf of any user via the ``UserId`` in
   the URL. The ``UserContext`` dataclass carries both the acting
   user's id AND the auth token; per-adapter logic chooses which to
   use. The engine just hands the context through.

Identifier glossary
-------------------

* ``backend`` - "plex" | "jellyfin" | "emby". Set as a class attribute
  on each adapter implementation.
* ``backend_user_id`` - per-backend stable user identifier. Plex.tv
  numeric userID for Plex; server-local UUID for Jellyfin / Emby.
  Persisted on ``managed_users.backend_user_id`` (migration v10).
* ``backend_item_id`` - per-backend stable item identifier. Plex
  ratingKey string for Plex; item GUID for Jellyfin / Emby. Local to a
  given server.
* ``library_id`` - per-backend library identifier. Integer string for
  Plex section keys (Plex's ``section.key`` is a numeric string);
  GUID for Jellyfin / Emby MediaFolders.
* ``guids`` - tuple of normalized upstream identifiers
  (``imdb://tt...``, etc.) for cross-server / cross-backend matching.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

log = logging.getLogger("plexmigrate.services.adapters")


# ── Discovery dataclasses ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ServerIdentity:
    """Per-server metadata the engine queries via ``ping`` /
    ``server_identity`` for boot-time diagnostics and the dashboard."""
    machine_id: str            # backend-native server id (Plex machineIdentifier; Jellyfin Id)
    name: str                  # friendly name as reported by the server
    version: str
    owner_user_id: str         # backend-native id of the owner / admin account
    owner_display: str         # human-readable label for the owner

    def __str__(self) -> str:
        return f"{self.name} (v{self.version})"


@dataclass(frozen=True)
class LibrarySpec:
    """One library / section / MediaFolder."""
    library_id: str            # opaque
    name: str
    type: str                  # "movie" | "show" | "artist" | "photo" | "mixed" | "boxsets"
    item_count: Optional[int] = None


@dataclass(frozen=True)
class UserSpec:
    """One user known to the server. ``role`` distinguishes owner /
    admin (server account holder) from managed users (Plex Home members,
    Jellyfin / Emby local accounts)."""
    backend_user_id: str
    username: str              # backend-native handle (Plex handle, Jellyfin Name)
    display_name: str          # end user-visible label
    role: str                  # "owner" | "managed"
    is_admin: bool = False


# ── Item dataclasses ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ItemRef:
    """Lightweight reference used for write operations.

    ``backend_item_id`` is opaque per-backend. ``guids`` carries the
    normalized cross-server identity (after
    ``services/guid_translator.normalize_guids``). ``library_id`` and
    ``title`` are populated when known (always for items returned by
    ``iter_items``; may be empty for items the engine builds from a
    snapshot file before resolving against the destination).

    ``file_path`` is the on-disk path the source server reported for
    this item (e.g. ``D:\\Music\\Artist\\Album\\01 Song.flac``). Used
    by the playlist-copy orchestrator's path-tail fallback resolver
    when the GUID-based match misses (common on music tracks that
    have no public metadata GUIDs). Empty when the source adapter
    didn't surface a path.

    ``item_type`` / ``artist`` / ``show_title`` carry the metadata
    fuzzy-title matching needs to disambiguate same-titled tracks
    across artists or same-titled episodes across shows. Empty when
    the source adapter didn't surface them, so a caller that omits
    them gets the historical behaviour."""
    backend_item_id: str
    guids: Tuple[str, ...] = ()
    library_id: Optional[str] = None
    title: str = ""
    file_path: str = ""
    item_type: str = ""        # "movie" | "episode" | "track" | "album" | ...
    artist: str = ""           # grandparentTitle for tracks
    show_title: str = ""       # grandparentTitle for episodes
    album: str = ""            # parentTitle for tracks; used as a
                               # fuzzy-ambiguity tiebreaker so two
                               # same-titled tracks (e.g. "Car Radio"
                               # by twenty one pilots on both Vessel +
                               # the deluxe edition) can be
                               # disambiguated by the source's album.
    # The parent item's cross-server GUID - the series GUID for an
    # episode, the artist GUID for a track. Preferred over show_title
    # / artist string matching by the resolver's hierarchy tier
    # because a GUID is immune to localized titles + agent drift.
    # Empty for movies + backends that don't expose a parent GUID.
    grandparent_guid: str = ""


@dataclass(frozen=True)
class ItemSnapshot:
    """Full per-item record returned by ``iter_items``.

    Carries everything the engine needs to serialize an item to the
    snapshot's per-row dict (see ``item_snapshot_to_engine_dict``) and
    everything the restorer needs to drive item-level write decisions.

    ``view_offset_ms`` is always milliseconds in this layer; per-backend
    tick conversions live inside the adapters (Jellyfin / Emby use
    100-nanosecond ticks).

    Hierarchy fields (``show_title``, ``season_title``, ``artist``,
    ``album``) are populated for episode / track types so the snapshot
    dict can reconstruct the show -> season -> episode tree (and
    artist -> album -> track) on the destination. Empty for movie /
    standalone item types."""
    backend_item_id: str
    guids: Tuple[str, ...]
    library_id: str
    title: str
    type: str                  # "movie" | "episode" | "track" | "show" | "album" | ...
    year: Optional[int] = None
    file_path: Optional[str] = None
    view_count: int = 0
    last_viewed_at: Optional[float] = None  # unix epoch seconds
    view_offset_ms: int = 0
    user_rating: Optional[float] = None     # 0.0 - 10.0
    # The favorite face of the
    # neutral per-user affinity record. None when the backend has no
    # favorite concept (Plex); True / False for Jellyfin / Emby.
    is_favorite: Optional[bool] = None
    added_at: Optional[float] = None        # unix epoch; item-added-to-library timestamp
    # TV hierarchy (empty for non-episodes).
    show_title: str = ""
    season_title: str = ""
    # Numeric episode indices. Plex emits these via
    # ``episode.parentIndex`` (season number) and ``episode.index``
    # (episode number); Jellyfin/Emby return ``ParentIndexNumber`` and
    # ``IndexNumber`` on the same Items response. Without these,
    # cross-server episode matching degrades to fuzzy-title which is
    # unreliable when shows have similarly-named episodes ("Pilot" in
    # Season 1 of every TV show). The restore-time matcher uses them
    # as tiebreakers after GUID + show_title match.
    season_index: Optional[int] = None
    episode_index: Optional[int] = None
    # Music hierarchy (empty for non-tracks).
    artist: str = ""
    album: str = ""
    # The parent item's cross-server GUID (series GUID for episodes,
    # artist GUID for tracks). The resolver's hierarchy tier prefers a
    # parent-GUID match over the title/index columns. Empty for
    # movies + backends that don't expose a parent GUID.
    grandparent_guid: str = ""

    def as_ref(self) -> ItemRef:
        return ItemRef(
            backend_item_id=self.backend_item_id,
            guids=self.guids,
            library_id=self.library_id,
            title=self.title,
            item_type=self.type,
            artist=self.artist,
            show_title=self.show_title,
            album=self.album,
            grandparent_guid=self.grandparent_guid,
        )


def item_snapshot_to_engine_dict(
    snap: ItemSnapshot,
    *,
    user: str = "",
) -> dict:
    """Convert :class:`ItemSnapshot` to the per-item dict the snapshot
    engine has historically emitted via ``services/resolver.serialize_item``.

    Output keys map to the canonical engine shape:

      title, type, rating_key, guids, filepath, view_count,
      last_viewed_at, view_offset, user_rating, added_at, user,
      library_section_id, show_title, season_title, artist, album

    Field-name notes:
    - ``rating_key`` is the backend-native item id. For Plex this is
      the plexapi ratingKey; for Jellyfin / Emby it's the item GUID.
      The name is kept for backwards-compat with the existing snapshot
      consumers in ``services/restorer.py`` and ``server/snapshot_serializer.py``.
    - ``view_offset`` is milliseconds (unchanged from the legacy shape).
    - ``last_viewed_at`` is the unix epoch; legacy serialize_item emitted
      a ``str(datetime)``. PR-Backends migrates to numeric epoch so the
      engine doesn't need to parse a string back into a comparable.
    """
    data: dict = {
        "title": snap.title,
        "type": snap.type,
        "rating_key": snap.backend_item_id,
        "guids": list(snap.guids),
        "filepath": snap.file_path or "",
        "view_count": int(snap.view_count or 0),
        "last_viewed_at": snap.last_viewed_at,
        "view_offset": int(snap.view_offset_ms or 0),
        "user_rating": snap.user_rating,
        # The neutral favorite face. None for Plex (no favorite
        # concept); bool for J/E.
        "is_favorite": snap.is_favorite,
        "added_at": str(snap.added_at) if snap.added_at else "",
        "user": user,
        "library_section_id": snap.library_id,
    }
    if snap.type == "episode":
        data["show_title"] = snap.show_title
        data["season_title"] = snap.season_title
        # Include numeric episode indices when available
        # so cross-server matching can disambiguate same-titled
        # episodes across shows / seasons. Mirrors what the Plex
        # engine emits via ``episode.parentIndex`` (season) and
        # ``episode.index`` (episode).
        if snap.season_index is not None:
            data["parent_index"] = int(snap.season_index)
        if snap.episode_index is not None:
            data["episode_index"] = int(snap.episode_index)
    elif snap.type == "track":
        data["artist"] = snap.artist
        data["album"] = snap.album
    # Parent GUID rides on every
    # hierarchy-bearing item type (episode + track). Emitted only
    # when populated so movie rows + GUID-less backends stay clean.
    if snap.grandparent_guid:
        data["grandparent_guid"] = snap.grandparent_guid
    return data


# ── Per-user context ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UserContext:
    """Identifies which user is performing a read / write.

    For Plex this is per-user token: ``auth_token`` is the user's own
    token (obtained via ``user.get_token(machineIdentifier)`` or PIN
    sign-in). For Jellyfin / Emby the admin token suffices to read /
    write on behalf of any user via the ``UserId`` in the URL; the
    adapter uses ``backend_user_id`` to scope the call and ignores
    ``auth_token`` if ``is_admin`` is true.

    The engine builds one ``UserContext`` per user it intends to
    snapshot / restore and threads it through."""
    backend_user_id: str
    username: str              # for logging only
    auth_token: str
    is_admin: bool = False     # true when auth_token is the admin token
    # CONSOLE-07: set when a non-owner per-user context could not get
    # the user's own token and fell back to the admin/owner token; a
    # write under it lands under the OWNER, not the requested user.
    admin_fallback: bool = False


# ── Write result ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WriteResult:
    """Outcome of a per-item write call.

    ``unsupported`` distinguishes "the backend can't do this" (e.g.
    ``set_favorite`` on Plex) from "the call failed" (network error,
    auth, etc.). The run summary breaks these out separately so a Plex
    destination doesn't show 1000 "failed" rating-favorite writes."""
    success: bool
    detail: str = ""
    unsupported: bool = False

    @classmethod
    def ok(cls, detail: str = "") -> "WriteResult":
        return cls(success=True, detail=detail)

    @classmethod
    def fail(cls, detail: str) -> "WriteResult":
        return cls(success=False, detail=detail)

    @classmethod
    def not_supported(cls, detail: str = "") -> "WriteResult":
        return cls(success=False, unsupported=True, detail=detail)


# ── Playlists & Collections ────────────────────────────────────────────────

@dataclass(frozen=True)
class PlaylistSpec:
    """One playlist as enumerated by the engine.

    ``is_smart`` rows are skipped on transfer today; the criteria are
    preserved in ``smart_filter_json`` (Plex schema) so the end user
    can manually recreate. Jellyfin / Emby smart-playlist support is
    plugin-mediated and not portable; same behaviour applies.

    Library-attribution fields surface WHICH library each playlist's
    items belong to, so the Playlist Management UI can group per-user
    playlists by source library and treat video / photo playlists as
    first-class alongside audio:

      * ``playlist_type``: ``"audio"`` / ``"video"`` / ``"photo"`` /
        ``"mixed"`` / ``""``. On Plex this comes from
        ``Playlist.playlistType``; on Jellyfin / Emby it comes from
        ``MediaType``. Unknown values fall through to ``""``.
      * ``primary_library_id``: backend-native id of the library
        section that holds the majority of the playlist's items
        (None when undeterminable — empty playlist, or every item
        lacks a library tag).
      * ``primary_library_name``: friendly name for the same library
        (None when undeterminable). The UI prefers name; id stays
        for stable grouping when names collide.
    """
    playlist_id: str
    name: str
    is_smart: bool = False
    smart_filter_json: Optional[str] = None
    items: Tuple[ItemRef, ...] = ()
    playlist_type: str = ""
    primary_library_id: Optional[str] = None
    primary_library_name: Optional[str] = None


@dataclass(frozen=True)
class CollectionSpec:
    """One collection / BoxSet.

    ``library_id`` is populated for Plex (collections are library-scoped)
    and None for Jellyfin / Emby (BoxSets are server-wide). The
    cross-backend transfer path applies the library-name prefix when
    crossing from library-scoped to server-wide per the locked
    D-COL-SCOPE decision."""
    collection_id: str
    name: str
    library_id: Optional[str] = None
    items: Tuple[ItemRef, ...] = ()


# ── User policy (Jellyfin / Emby only) ──────────────────────────────────────

@dataclass(frozen=True)
class UserPolicy:
    """Permission shape for newly-created Jellyfin / Emby users. Plex
    cannot create users via API so this is unused on PlexAdapter."""
    is_administrator: bool = False
    is_disabled: bool = False
    enable_all_folders: bool = True
    enabled_folder_ids: Tuple[str, ...] = ()


# ── The adapter ABC ─────────────────────────────────────────────────────────

class MediaServerAdapter(ABC):
    """Backend-agnostic interface every backend (Plex / Jellyfin / Emby)
    implements. The engine calls only this surface.

    Subclasses set the ``backend`` class attribute to one of "plex",
    "jellyfin", "emby". The factory in ``server/server_registry.py``
    instantiates the right subclass based on ``ServerView.service_type``.
    """

    backend: str = "unknown"

    # ── Discovery ──────────────────────────────────────────────────────

    @abstractmethod
    def ping(self) -> bool:
        """Cheap liveness check. Returns True if the server responded."""

    @abstractmethod
    def server_identity(self) -> ServerIdentity:
        """Return server metadata + owner identity. Called once per
        connection lifecycle; result may be cached on the adapter."""

    @abstractmethod
    def list_libraries(self) -> List[LibrarySpec]:
        """Every library / section / MediaFolder visible to the
        adapter's admin token."""

    @abstractmethod
    def list_users(self) -> List[UserSpec]:
        """Every user known to the server (owner + managed). For Plex
        this combines ``server.systemAccounts()`` with the friend list
        for share-state context. For Jellyfin / Emby it's
        ``GET /Users``."""

    def probe_user(self, username: str) -> str:
        """Return one of ``'ok' | 'auth_error' | 'unreachable' | 'unknown'``
        for the named user. Cheap; the user-activity sweeper calls
        this once per managed user per cycle.

        Default implementation: best-effort fallback that calls
        ``list_users()`` and checks whether the username appears. Real
        adapters override with backend-native probes:
          - Plex: ``myPlexAccount().user(username)``
          - Jellyfin / Emby: ``GET /Users/{userId}`` with admin key

        Override-friendly: keep this method on the ABC so test
        fixtures and any future backend adapter that hasn't shipped
        a specific probe yet still gets a working (if slower)
        default.
        """
        try:
            roster = self.list_users() or []
        except Exception as exc:
            log.debug(
                "probe_user default fallback: list_users failed: %s",
                exc,
            )
            return "unreachable"
        needle = (username or "").strip().lower()
        for u in roster:
            uname = (getattr(u, "username", "") or "").strip().lower()
            if uname == needle:
                return "ok"
        # A user absent from the roster is reported as auth_error by
        # design - the user-activity sweeper treats a removed/unknown
        # user the same as a credential failure so it still counts
        # toward the inactivity-tombstone threshold. The PlexAdapter
        # override maps a 404 the same way for the same reason. (This
        # was the ADAPT-08 finding; on review the conflation is the
        # intended contract - pinned by test_adapter_probe_user_logger
        # - so the default is left as-is.)
        return "auth_error"

    # ── Item enumeration ───────────────────────────────────────────────

    def resolve_by_guids(
        self,
        guids: Tuple[str, ...],
        *,
        library_id: Optional[str] = None,
        item_type_hint: str = "",
    ) -> Optional[str]:
        """Given a tuple of normalized cross-server GUIDs (from
        :func:`services.guid_translator.normalize_guids`), return the
        backend-native item id on this server, or ``None`` if no match.

        Used by the restorer's adapter path to convert a source
        snapshot row's ``guids`` into the destination's
        ``backend_item_id`` for per-item write operations.
        ``library_id`` narrows the search when known.

        Default returns ``None`` (unsupported); per-backend
        implementations override."""
        return None

    def resolve_by_full_path(
        self,
        file_path: str,
        *,
        library_id: Optional[str] = None,
        item_type_hint: str = "",
    ) -> Optional[str]:
        """Tier 2 of the playlist-copy resolution chain. Exact-match
        the absolute ``file_path``
        against the destination server's items. Useful when the source
        + destination servers share an identical mount-point shape
        (typical for NAS-backed setups where the same path is mounted
        at the same prefix on both Plexes).

        Returns the backend-native item id on hit, ``None`` on miss.
        Default returns ``None`` (unsupported); per-backend
        implementations override."""
        return None

    def resolve_by_path_tail(
        self,
        file_path: str,
        *,
        tail_components: int = 3,
        item_type_hint: str = "",
        library_id: Optional[str] = None,
    ) -> Optional[str]:
        """Tier 3 of the playlist-copy resolution chain. Match by the
        last ``tail_components`` path components (default 3,
        ``artist/album/song``) so a track resolves across servers that
        share an on-disk layout beneath a different mount root.

        Returns the backend-native item id on hit, ``None`` on miss.
        Default returns ``None`` (unsupported); per-backend
        implementations override."""
        return None

    def resolve_by_fuzzy_title(
        self,
        title: str,
        *,
        item_type: str = "",
        artist: str = "",
        show_title: str = "",
        album: str = "",
        source_file_path: str = "",
        ambiguous_behavior: str = "strict",
        library_id: Optional[str] = None,
        item_type_hint: str = "",
    ) -> Optional[List[str]]:
        """Tier 4 (last-resort) of the playlist-copy resolution chain.
        Title-based search filtered by
        ``item_type``, ``artist`` (tracks), ``show_title`` (episodes).
        Critical for music libraries because Plex doesn't expose
        ``getByGuid`` for ``mbid://`` MusicBrainz IDs.

        Ambiguity handling:
          1. First narrow candidates by ``album`` (tracks) when the
             source surfaced it AND the dest exposes parentTitle.
          2. Then narrow by ``source_file_path`` — match candidates
             whose own ``media[0].parts[0].file`` shares the longest
             trailing path segment (artist/album/file). Picks the
             best-aligned candidate when the source's path is known.
          3. If multiple candidates STILL survive, apply
             ``ambiguous_behavior``:
               * ``'strict'`` — return None (caller records miss)
               * ``'first'`` — return [first.ratingKey]
               * ``'all'`` — return [every candidate's ratingKey]

        Returns ``None`` on miss / strict-refusal, OR a list of
        backend-native item ids (one for unique/first; many for
        'all' mode). The caller turns each id into a destination
        write."""
        return None

    @abstractmethod
    def iter_items(
        self,
        library_id: str,
        *,
        include_watched_only: bool = False,
        include_rated_only: bool = False,
        user_context: Optional[UserContext] = None,
    ) -> Iterator[ItemSnapshot]:
        """Yield every item in ``library_id``.

        ``include_watched_only`` and ``include_rated_only`` are server-
        side filters where supported; adapters fall back to client-side
        filtering on a single full enumeration when the filter would
        otherwise multi-scan the library.

        ``user_context`` scopes the read to one user's view of the
        library (so per-user view_count / view_offset / user_rating
        come back correctly). When None, the adapter's own admin token
        is used (owner's view)."""

    # ── Per-user watch / rating writes ─────────────────────────────────

    @abstractmethod
    def set_watched(
        self,
        item_ref: ItemRef,
        *,
        view_count: int,
        last_viewed_at: Optional[float],
        user_context: UserContext,
        current_view_count: Optional[int] = None,
    ) -> WriteResult:
        """Record one watch event. ``view_count`` is the absolute target
        count to write on the destination.

        Exact-target contract:
          * Jellyfin / Emby: a single ``POST .../UserData`` call sets
            the exact ``PlayCount`` regardless of current state, so
            ``current_view_count`` is ignored.
          * Plex: ``/:/scrobble`` only +1's and ``/:/unscrobble`` only
            zeroes; there is no "set to N" endpoint. When
            ``current_view_count`` is provided, the adapter does the
            exact math:
              - target == 0           -> single unscrobble
              - target == current     -> noop (already correct)
              - target > current      -> (target - current) scrobbles
              - 0 < target < current  -> unscrobble + target scrobbles
            When ``current_view_count`` is None (legacy callers), the
            adapter falls back to the "loop ``view_count`` scrobbles"
            semantics and the caller is responsible for having pre-
            computed any delta. ``state.VIEWCOUNT_INCREMENT_CAP`` is
            still respected in either mode."""

    def get_current_view_count(
        self,
        item_ref: ItemRef,
        *,
        user_context: UserContext,
    ) -> Optional[int]:
        """Return the destination's CURRENT play count for ``item_ref``
        under ``user_context``, or None when the adapter can't read it
        cheaply.

        Used by the restorer's Replace mode to compute the exact-target
        delta on Plex (where the API only exposes +1 / zero primitives).
        Jellyfin / Emby ``set_watched`` writes exact via the UserData
        endpoint, so they have no use for the read and the base-class
        default returns None.

        Implementations should bound the cost: a single GET is
        acceptable; an enumeration walk is not. Return None on failure
        rather than raising so the caller can fall back to delta-mode."""
        return None

    def get_view_state(
        self,
        item_ref: ItemRef,
        *,
        user_context: UserContext,
    ) -> Optional[Tuple[int, Optional[float]]]:
        """Return ``(view_count, last_viewed_at_epoch)`` for ``item_ref``
        under ``user_context``, or None when the count can't be read
        cheaply. ``last_viewed_at_epoch`` may be None even when the
        count is known.

        Used by the sync engine's ``latest_wins`` conflict policy,
        which needs a per-side last-changed timestamp to decide which
        side's count is newer (JOBS-03). The base implementation
        delegates to :meth:`get_current_view_count` and reports no
        timestamp; an adapter that can cheaply read the last-viewed
        time should override this so ``latest_wins`` works against it.
        Same single-GET cost bound as :meth:`get_current_view_count`."""
        vc = self.get_current_view_count(
            item_ref, user_context=user_context,
        )
        if vc is None:
            return None
        return (vc, None)

    @abstractmethod
    def set_resume_position(
        self,
        item_ref: ItemRef,
        offset_ms: int,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """Set the in-progress resume position. ``offset_ms`` is always
        milliseconds at this layer; the adapter converts to backend-
        native units (Plex ms, Jellyfin / Emby 100-ns ticks)."""

    @abstractmethod
    def set_rating(
        self,
        item_ref: ItemRef,
        rating: float,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """Set the user's personal rating in 0.0-10.0. Adapters scale
        to backend-native ranges as needed."""

    def set_favorite(
        self,
        item_ref: ItemRef,
        favorite: bool,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """Set the per-user favorite toggle. Default is unsupported
        (Plex). JellyfinAdapter / EmbyAdapter override to call the
        ``/Users/{id}/FavoriteItems/{id}`` endpoint."""
        return WriteResult.not_supported(
            f"backend {self.backend!r} does not expose a per-user favorite toggle"
        )

    # ── Playlists ──────────────────────────────────────────────────────

    @abstractmethod
    def list_playlists(self, user_context: UserContext) -> List[PlaylistSpec]:
        """Every playlist visible to the user. Smart playlists are
        included with ``is_smart=True`` and their criteria preserved in
        ``smart_filter_json`` where the backend exposes them; the
        engine's transfer path skips smart playlists with a log line."""

    def get_playlist_items(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> Tuple[ItemRef, ...]:
        """Return the ordered items of one playlist as ItemRef tuples.

        Backs the
        Playlist Management copy flow + cache layer. The default
        implementation derives items from ``list_playlists`` (Plex
        returns items inline, so no extra fetch is needed). Backends
        that need a per-playlist fetch (Jellyfin / Emby's
        ``GET /Playlists/{id}/Items?UserId=...``) override this for
        efficiency."""
        for spec in self.list_playlists(user_context):
            if spec.playlist_id == playlist_id:
                return spec.items
        return ()

    def get_playlist(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> Optional[PlaylistSpec]:
        """Return a single playlist's full PlaylistSpec (name + is_smart
        + items) WITHOUT enumerating the user's other playlists.

        Calling ``list_playlists`` to look up one playlist's name +
        is_smart and then ``get_playlist_items`` to re-fetch its
        items is wasteful: the first call eagerly fetches items for
        EVERY playlist the user has, which can add a multi-minute
        delay. This method fetches ONE playlist directly and
        populates one PlaylistSpec.

        Default returns ``None`` (caller falls back to the old slow
        path). PlexAdapter overrides via ``server.fetchItem(rk)``;
        Jellyfin / Emby override via their per-id Playlist GET."""
        return None

    @abstractmethod
    def create_playlist(
        self,
        name: str,
        items: List[ItemRef],
        *,
        user_context: UserContext,
    ) -> str:
        """Create a new playlist and return its ``playlist_id``. The
        adapter handles chunking when the backend has URL-length limits
        (Plex's ~8KB ``uri=`` cap)."""

    @abstractmethod
    def add_to_playlist(
        self,
        playlist_id: str,
        items: List[ItemRef],
        *,
        user_context: UserContext,
    ) -> int:
        """Append items to an existing playlist; returns count added."""

    def delete_playlist(
        self,
        playlist_id: str,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        """Delete a playlist by id. Used by Replace-mode restore to
        prune dest-only rows so the destination becomes an exact mirror
        of the source.

        Default returns unsupported; per-backend adapters override.
        The orchestrator counts unsupported results separately so the
        run summary makes the gap visible without aborting the run."""
        return WriteResult.not_supported(
            f"backend {self.backend!r} does not expose a playlist-delete API"
        )

    # ── Collections ────────────────────────────────────────────────────

    @abstractmethod
    def list_collections(
        self, library_id: Optional[str] = None,
    ) -> List[CollectionSpec]:
        """Every collection. ``library_id`` filters to one library on
        backends where collections are library-scoped (Plex). Jellyfin /
        Emby BoxSets are server-wide; the parameter is ignored
        gracefully."""

    @abstractmethod
    def create_collection(
        self,
        name: str,
        items: List[ItemRef],
        *,
        library_id: Optional[str] = None,
    ) -> str:
        """Create a collection / BoxSet and return its id. ``library_id``
        is required on Plex; optional on Jellyfin / Emby."""

    @abstractmethod
    def add_to_collection(
        self,
        collection_id: str,
        items: List[ItemRef],
    ) -> int:
        """Append items to a collection; returns count added."""

    def delete_collection(
        self,
        collection_id: str,
    ) -> WriteResult:
        """Delete a collection / BoxSet by id. Used by Replace-mode
        restore to prune dest-only rows. Default returns unsupported."""
        return WriteResult.not_supported(
            f"backend {self.backend!r} does not expose a collection-delete API"
        )

    # ── User management (Jellyfin / Emby only) ─────────────────────────

    def create_user(
        self,
        username: str,
        *,
        password: str,
        is_admin: bool = False,
        policy: Optional[UserPolicy] = None,
    ) -> Optional[UserSpec]:
        """Create a new user on this server. Returns the new ``UserSpec``
        (with backend_user_id populated by the server) or None if the
        backend does not support API user creation (Plex always returns
        None)."""
        return None

    def set_user_password(
        self, backend_user_id: str, *, password: str,
    ) -> WriteResult:
        """Set a user's password. Unsupported on Plex (Plex.tv-managed)."""
        return WriteResult.not_supported(
            f"backend {self.backend!r} does not expose a user-password API"
        )

    def delete_user(self, backend_user_id: str) -> WriteResult:
        """Remove a user. Unsupported on Plex."""
        return WriteResult.not_supported(
            f"backend {self.backend!r} does not expose user deletion"
        )


__all__ = [
    "ServerIdentity",
    "LibrarySpec",
    "UserSpec",
    "UserPolicy",
    "ItemRef",
    "ItemSnapshot",
    "UserContext",
    "WriteResult",
    "PlaylistSpec",
    "CollectionSpec",
    "MediaServerAdapter",
    "item_snapshot_to_engine_dict",
]
