"""
PlexAdapter: thin wrapper around plexapi implementing
:class:`services.adapters.MediaServerAdapter`.

This is a SHIM, not a rewrite. Every method delegates to existing
plexapi code paths so the foundation PR introduces zero behaviour
change. The engine post-migration calls ``adapter.iter_items(...)``
instead of ``server.library.section(...).search()`` directly, but the
underlying plexapi calls (and therefore the HTTP traffic) are
identical.

The class is constructed with a connected ``PlexServer`` instance and
its base URL + admin token (needed for the direct ``/:/scrobble`` /
``/:/progress`` / ``/:/rate`` HTTP write helpers that bypass plexapi).
Construction happens in ``server/server_registry.py:connect_registered_server``
once per connection; the resulting adapter is stored on the
``ServerConnection`` dataclass alongside other connection metadata.

Class layout
------------
``PlexAdapter`` is structurally split across mixin files for sanity;
the public interface is unchanged. Each mixin owns a cohesive method
group, all backed by the shared instance state ``__init__`` sets up
here:

* :class:`PlexResolveMixin` (``_plex_resolve``) — the 4-tier
  cross-server item resolvers + their helpers + test-reset hooks.
* :class:`PlexWritesMixin` (``_plex_writes``) — per-user scrobble /
  rating / resume HTTP writes + the read-side helpers that drive
  exact-target writes.
* :class:`PlexContainersMixin` (``_plex_containers``) — regular
  playlist + collection CRUD + the chunked add helpers + the
  per-user accountID resolver used by ``list_playlists``.
* :class:`PlexSmartPlaylistMixin` (``_plex_smart``) — smart-playlist
  read / write / vocabulary preflight.
* :class:`MirrorResolveMixin` (``_mirror_resolve``) — backend-agnostic
  mirror-first wrappers shared with the Jellyfin / Emby adapters.

Connection / lifecycle / iteration methods + the per-user PlexServer
factory + the module-level plex-coupled helpers stay here.

Capability notes (PlexAdapter-specific)
---------------------------------------
* ``set_favorite`` is unsupported (Plex has no per-user favorite
  toggle; ratings are the only per-user signal). Returns
  ``WriteResult.not_supported``.
* ``create_user`` / ``set_user_password`` / ``delete_user`` are
  unsupported (Plex sharing is managed via Plex.tv UI only, not via
  the Plex Media Server REST API). The defaults from
  ``MediaServerAdapter`` already return ``None`` / unsupported.
* ``set_watched`` translates the absolute ``view_count`` target into N
  ``/:/scrobble`` increment calls, capped at
  ``services.state.VIEWCOUNT_INCREMENT_CAP`` to match the existing
  restorer behaviour.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

from plexapi.server import PlexServer

from services.translation.guid_translator import normalize_guids
from services.resolver import (
    _all_guids,
    _disable_autoreload,
    _normalize_path_parts,
    _safe_file_path,
    _section_leaf_items,
)  # noqa: F401

from . import (
    ItemSnapshot,
    LibrarySpec,
    MediaServerAdapter,
    ServerIdentity,
    UserContext,
    UserSpec,
)
from ._mirror_resolve import MirrorResolveMixin
from ._plex_containers import PlexContainersMixin
from ._plex_resolve import PlexResolveMixin
from ._plex_smart import PlexSmartPlaylistMixin
from ._plex_writes import PlexWritesMixin


log = logging.getLogger("plexmigrate.services.adapters.plex")


class PlexAdapter(
    PlexResolveMixin,
    PlexWritesMixin,
    PlexContainersMixin,
    PlexSmartPlaylistMixin,
    MirrorResolveMixin,
    MediaServerAdapter,
):
    """Plex implementation of :class:`MediaServerAdapter`.

    Wraps a connected ``plexapi.server.PlexServer`` instance. The
    adapter holds the server, base URL, and admin token. Per-user
    operations switch between the admin server and a per-user
    ``PlexServer`` instance carried on the ``UserContext`` via plexapi's
    own per-user impersonation surface (the user's own access token
    creates a per-user PlexServer).
    """

    backend: str = "plex"

    def __init__(
        self,
        server: PlexServer,
        *,
        base_url: str,
        admin_token: str,
        machine_id: Optional[str] = None,
    ) -> None:
        self._server = server
        self._base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        self._machine_id_cached = (
            machine_id
            or getattr(server, "machineIdentifier", "")
            or ""
        )
        # Per-user PlexServer cache so a per-user-token write doesn't
        # pay a fresh HTTP
        # handshake on every call. Keyed by the user's auth_token
        # (Plex's per-user X-Plex-Token). The admin server is held
        # separately on ``self._server`` and never enters this cache.
        self._per_user_servers: dict = {}
        # Path-tail resolver index. Built lazily on first
        # ``resolve_by_path_tail`` call; cached per (adapter, depth)
        # for the lifetime of this adapter instance. See that method's
        # docstring for the matching rationale.
        self._path_tail_indexes: Dict[str, Dict[str, str]] = {}
        # When several sections match an item_type hint, lock onto the
        # FIRST section that resolves a real match so subsequent items
        # in the same batch don't probe sibling music libraries that
        # won't have the source items either. Keyed by lowercased
        # item_type_hint -> section.key (str). Cleared by
        # ``clear_library_of_truth_for_tests``.
        self._library_of_truth: Dict[str, str] = {}
        # Cache the per-(section, item_type, artist/show) track set so
        # 50 tracks by one artist cost ONE Plex query, not 50. Key:
        #   (section.key, item_type, parent_group_title)
        # Value:
        #   { lowercased_title: [Track, ...] }
        # The parent_group_title is the artist for tracks, the show
        # for episodes, "" for items with no group.
        self._artist_track_indexes: Dict[Tuple[str, str, str], Dict[str, List[Any]]] = {}
        # Track per-(section_key, guid_scheme) hit/miss counts. After
        # ``_GUID_BLACKLIST_THRESHOLD`` consecutive misses with 0
        # hits, stop probing that combination entirely for the rest
        # of this adapter's lifetime. Plex's mbid:// resolution is the
        # typical victim: music GUIDs never match Plex's getByGuid
        # index, so every probe wastes ~3s on a guaranteed miss.
        self._guid_attempt_state: Dict[Tuple[str, str], Dict[str, int]] = {}
        # Multiple worker threads mutate these caches concurrently
        # during per-item resolution. Most race outcomes are benign
        # (e.g. two threads building the same artist index = wasted
        # CPU, not wrong data). The lock makes the writes deterministic
        # so debugging stays sane.
        self._cache_lock = threading.Lock()
        # Per-cache-key build-in-progress events so only the FIRST
        # thread builds the index; others wait on the event and then
        # read the populated cache. Without this, parallel workers
        # race past the cache check and every one rebuilds the same
        # ~100s index. Keyed by the path-tail cache_key string.
        self._index_build_events: Dict[str, threading.Event] = {}
        # Cache the raw sections list once per adapter lifetime;
        # per-item filtering happens against this cache. Without it
        # every per-item resolver calls
        # self._server.library.sections() over the wire (~200ms per
        # call, several calls per item) - tens of seconds wasted on
        # the same fetch.
        self._sections_cache: Optional[List[Any]] = None
        # Username -> local SystemAccount.id map. Built lazily
        # on first ``list_playlists`` call. Plex's per-playlist
        # ``accountID`` attribute is the LOCAL account id (1, 2, 3, ...)
        # set by the server, NOT the Plex.tv user id we carry in
        # ``backend_user_id``. To filter playlists down to a specific
        # Plex Home user via the admin token, we need that local id.
        # See ``_local_account_id_for_username`` for the lookup details.
        self._local_account_id_by_username: Optional[Dict[str, int]] = None

    # ── Discovery ──────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Cheap liveness check via plexapi's session. ``PlexServer``
        was already constructed (which probes ``/identity``), so this
        is a no-op confirmation."""
        try:
            return bool(getattr(self._server, "machineIdentifier", ""))
        except Exception:
            return False

    def server_identity(self) -> ServerIdentity:
        owner_email = ""
        owner_user_id = ""
        try:
            account = self._server.myPlexAccount()
            owner_email = (getattr(account, "email", "") or "").strip()
            for attr in ("id", "userID", "userid"):
                val = getattr(account, attr, "") or ""
                if val:
                    owner_user_id = str(val).strip()
                    break
        except Exception:
            pass
        return ServerIdentity(
            machine_id=self._machine_id_cached,
            name=getattr(self._server, "friendlyName", "") or "",
            version=getattr(self._server, "version", "") or "",
            owner_user_id=owner_user_id,
            owner_display=owner_email or "Plex Owner",
        )

    def list_libraries(self) -> List[LibrarySpec]:
        out: List[LibrarySpec] = []
        for section in self._server.library.sections():
            count: Optional[int] = None
            try:
                count = int(getattr(section, "totalSize", 0)) or None
            except Exception:
                count = None
            out.append(LibrarySpec(
                library_id=str(getattr(section, "key", "")),
                name=getattr(section, "title", "") or "",
                type=getattr(section, "type", "") or "",
                item_count=count,
            ))
        return out

    def list_users(self) -> List[UserSpec]:
        """Owner + every SystemAccount on the server.

        Mirrors ``server/server_registry.py:get_server_users``;
        owner-vs-managed detection lives in the shared helper at
        :mod:`services.identity.plex_owner_identity` so both surfaces stay in
        lockstep when new identification signals are added."""
        from services.identity.plex_owner_identity import (
            derive_owner_identifiers,
            dedupe_owner_against_managed,
            is_owner_system_account,
        )
        out: List[UserSpec] = []
        owner_ids = {"email": "", "username": "", "account_id": "", "email_local": ""}
        try:
            account = self._server.myPlexAccount()
            owner_ids = derive_owner_identifiers(account)
        except Exception as exc:
            log.debug("myPlexAccount unavailable: %s", exc)

        if owner_ids["email"]:
            out.append(UserSpec(
                backend_user_id="",  # populated by the share-state refresh
                username=owner_ids["email"],
                display_name=owner_ids["email"],
                role="owner",
                is_admin=True,
            ))

        try:
            sys_accts = self._server.systemAccounts() or []
        except Exception as exc:
            log.debug("systemAccounts() unavailable: %s", exc)
            sys_accts = []

        for acct in sys_accts:
            name = (getattr(acct, "name", "") or "").strip()
            if not name:
                continue
            # The shared helper covers id==1 + username match + three
            # further signals (Plex.tv accountID, email full match,
            # email local-part match). Checking only id and username
            # is not enough: an operator-renamed owner SystemAccount
            # whose id != 1 would slip through and get appended as a
            # managed user.
            if is_owner_system_account(
                acct,
                owner_email=owner_ids["email"],
                owner_username=owner_ids["username"],
                owner_account_id=owner_ids["account_id"],
                owner_email_local=owner_ids["email_local"],
            ):
                continue
            out.append(UserSpec(
                backend_user_id="",
                username=name,
                display_name=name,
                role="managed",
                is_admin=False,
            ))
        # Defensive last-pass dedup. Catches edge cases where a
        # display-name we don't currently recognise as an owner signal
        # leaks through; the owner is identified positionally by
        # role="owner" and any matching managed row is dropped.
        # UserSpec is a frozen dataclass so dedupe_owner_against_managed
        # (which takes dict-shaped entries) doesn't apply directly;
        # do the same comparison inline.
        owner_keys = {
            s.lower().strip()
            for s in (owner_ids["email"], owner_ids["email_local"], owner_ids["username"])
            if s and s.strip()
        }
        if owner_keys:
            deduped: List[UserSpec] = []
            for spec in out:
                if spec.role == "managed" and spec.username.lower().strip() in owner_keys:
                    continue
                deduped.append(spec)
            out = deduped
        return out

    def probe_user(self, username: str) -> str:
        """Cheap "is this user OK"
        probe for the sweeper. Plex-native path uses
        ``myPlexAccount().user(username)`` which returns a User on
        success, raises NotFound on missing, and raises auth
        exceptions on token problems.

        Maps the outcomes to the cross-backend four-value enum so
        the sweeper records the right signal without backend-specific
        branching in the caller.
        """
        try:
            account = self._server.myPlexAccount()
        except Exception as exc:
            msg = str(exc).lower()
            if "401" in msg or "unauthorized" in msg or "token" in msg:
                # Admin-side problem; not specifically this user's
                # fault. Best classification is 'unreachable' so the
                # auto-tombstone-on-auth-error toggle doesn't fire on
                # an admin token issue.
                return "unreachable"
            if any(s in msg for s in ("connection", "timeout", "name resolution")):
                return "unreachable"
            return "unknown"
        try:
            user = account.user(username)
            return "ok" if user is not None else "auth_error"
        except Exception as exc:
            msg = str(exc).lower()
            if any(s in msg for s in ("not found", "404")):
                return "auth_error"
            if "401" in msg or "unauthorized" in msg:
                return "auth_error"
            if any(s in msg for s in ("connection", "timeout", "name resolution")):
                return "unreachable"
            return "unknown"

    # ── Mirror integration ──────────────────────────────────────────
    #
    # PlexAdapter exposes two provider methods that the per-server
    # metadata mirror's sync layer drives during sync_for_job. The
    # mirror caches the item universe so the 4 resolver methods below
    # can answer in milliseconds via SQL instead of seconds via live
    # Plex API. Each resolver tries the mirror FIRST; on miss, falls
    # through to the existing live walk so correctness is preserved.

    def iter_sections_for_mirror(self) -> List[Any]:
        """Yield :class:`services.mirror_sync.server_mirror.SectionInfo` for every
        library section on this server. Used by the sync layer to
        enumerate the universe before walking items.

        Picks up live_total_size + live_updated_at so the sync layer
        can decide between probe-only, delta, or full sync."""
        from services.mirror_sync.server_mirror import SectionInfo
        out: List[SectionInfo] = []
        try:
            sections = list(self._server.library.sections())
        except Exception as exc:
            log.warning(
                "iter_sections_for_mirror: library.sections() "
                "failed: %s", exc,
            )
            return out
        for s in sections:
            total = None
            updated = None
            try:
                total = int(getattr(s, "totalSize", 0) or 0) or None
            except (TypeError, ValueError):
                total = None
            try:
                ua = getattr(s, "updatedAt", None)
                if ua is not None:
                    updated = float(
                        ua.timestamp() if hasattr(ua, "timestamp")
                        else float(ua)
                    )
            except (TypeError, ValueError, AttributeError):
                updated = None
            out.append(SectionInfo(
                section_id=str(getattr(s, "key", "") or ""),
                name=getattr(s, "title", "") or "",
                section_type=getattr(s, "type", "") or "",
                live_total_size=total,
                live_updated_at=updated,
            ))
        return out

    def iter_section_items_for_mirror(
        self,
        section_id: str,
        since_ts: Optional[float] = None,
    ) -> Iterator[Any]:
        """Item provider used by the sync layer. Yields
        :class:`services.mirror_sync.server_mirror.ItemRow` for every leaf-level
        item in ``section_id``. When ``since_ts`` is supplied, items
        whose ``updatedAt`` is older are skipped (delta sync).
        """
        from services.mirror_sync.server_mirror import ItemRow
        try:
            section = next(
                (s for s in self._server.library.sections()
                 if str(getattr(s, "key", "") or "") == str(section_id)),
                None,
            )
        except Exception:
            section = None
        if section is None:
            return
        try:
            leaves = _section_leaf_items(section) or []
        except Exception as exc:
            log.warning(
                "iter_section_items_for_mirror: section walk failed "
                "for section_id=%s: %s", section_id, exc,
            )
            return
        for it in leaves:
            _disable_autoreload(it)
            rk = getattr(it, "ratingKey", None)
            if rk is None:
                continue
            # since_ts gating for delta-sync.
            if since_ts is not None:
                ua = getattr(it, "updatedAt", None)
                try:
                    ua_ts = float(
                        ua.timestamp() if hasattr(ua, "timestamp")
                        else float(ua) if ua is not None else 0.0
                    )
                except (TypeError, ValueError, AttributeError):
                    ua_ts = 0.0
                if ua_ts > 0 and ua_ts <= since_ts:
                    continue
            else:
                ua_ts = None
                ua = getattr(it, "updatedAt", None)
                try:
                    if ua is not None:
                        ua_ts = float(
                            ua.timestamp() if hasattr(ua, "timestamp")
                            else float(ua)
                        )
                except (TypeError, ValueError, AttributeError):
                    ua_ts = None

            item_type = getattr(it, "type", "") or ""
            # The parent item's cross-server GUID feeds the resolver's
            # hierarchy tier. Plex exposes ``grandparentGuid`` on
            # episodes (the series GUID) + tracks (the artist GUID)
            # for free on the bulk listing.
            _gpg = None
            if item_type in ("episode", "track"):
                _gpg = str(getattr(it, "grandparentGuid", "") or "") or None
            yield ItemRow(
                rating_key=str(rk),
                title=getattr(it, "title", "") or "",
                item_type=item_type,
                file_path=_safe_file_path(it) or None,
                guids=tuple(g for g in _all_guids(it) if g),
                artist=(getattr(it, "grandparentTitle", "") or None)
                if item_type == "track" else None,
                album=(getattr(it, "parentTitle", "") or None)
                if item_type == "track" else None,
                show_title=(getattr(it, "grandparentTitle", "") or None)
                if item_type == "episode" else None,
                season_number=getattr(it, "parentIndex", None),
                episode_number=getattr(it, "index", None),
                parent_rating_key=(
                    str(getattr(it, "parentRatingKey", "") or "")
                    or None
                ),
                grandparent_guid=_gpg,
                live_updated_at=ua_ts,
            )

    def _mirror_server_id(self) -> str:
        """The app registry UID this Plex server's mirror rows are
        keyed by.

        connect_registered_server stamps the registry UID
        (server_registry.make_server_id(), 'plex_<uuid4hex>') onto the
        plexapi server object so the resolver + this adapter agree on
        one key. The fallback to machineIdentifier covers ad-hoc
        adapter construction that bypassed the registry (tests,
        tooling); in that case the mirror simply will not be consulted
        unless it was also populated under that same id.

        The mirror-first lookup wrappers + the cold-start sync that
        consume this UID live in MirrorResolveMixin, shared with the
        Jellyfin / Emby adapters."""
        return (
            str(getattr(self._server, "_pmig_server_uid", "") or "")
            or self._machine_id_cached
            or ""
        )

    # ── Item enumeration ───────────────────────────────────────────────

    def iter_items(
        self,
        library_id: str,
        *,
        include_watched_only: bool = False,
        include_rated_only: bool = False,
        user_context: Optional[UserContext] = None,
    ) -> Iterator[ItemSnapshot]:
        """Yield every item in ``library_id`` as an ``ItemSnapshot``.

        When ``user_context`` is supplied AND the engine has built a
        per-user PlexServer, we re-resolve the section against that
        per-user server so per-user ``viewCount`` / ``userRating`` /
        ``viewOffset`` come back correctly. Otherwise the adapter's
        own admin server is used (owner's view).

        ``include_watched_only`` / ``include_rated_only`` use Plex's
        server-side ``viewCount__gt=0`` / ``userRating__gt=0`` filters
        when both are unset (or only one is set); both set together
        falls back to a single full enumeration + client-side filter
        per the existing snapshotter perf tunable
        ``watch_ratings_filter_strategy``."""
        server = self._server_for(user_context)
        section = server.library.sectionByID(int(library_id))
        libtype = getattr(section, "type", "")

        # Build the iterator. Mirrors snapshotter._bulk_fetch_for_filters
        # + snapshot_watch_history / snapshot_ratings logic but
        # collapsed: only the actually-needed filter combination runs.
        items_iter = self._search_for_filters(
            section, libtype,
            include_watched_only=include_watched_only,
            include_rated_only=include_rated_only,
        )

        for plex_item in items_iter:
            # plexapi partial objects auto-reload on first attr access
            # for fields not in the inline XML; the snapshot path
            # explicitly disables that to avoid an N+1.
            _disable_autoreload(plex_item)
            yield _plex_item_to_snapshot(plex_item, library_id=library_id)

    def _server_for(self, user_context: Optional[UserContext]) -> PlexServer:
        """Resolve which PlexServer instance to drive a read / write with.

        Returns the admin server (``self._server``) when:
          * ``user_context`` is None (no per-user scoping requested).
          * ``user_context.is_admin`` is True (the orchestrator
            explicitly chose to act as the admin).
          * ``user_context.auth_token`` matches the admin token (same
            token; building a parallel PlexServer would be wasteful).

        Otherwise constructs (and caches per token) a per-user
        PlexServer instance with ``user_context.auth_token``. This is
        what makes writes (create_playlist, set_watched, set_rating)
        attribute to the per-user account on Plex Home setups when the
        Playlist Management ``playlist_mgmt_plex_home_auth_mode``
        tunable is set to ``per_user_token`` (see
        :func:`services.playlist_copy._user_context_for`).

        Cache lifetime: process-lifetime, keyed by the user's
        X-Plex-Token (which is stable per (server, user) pair until
        the end user rotates it on plex.tv). A bad / rotated token
        surfaces as a ``plexapi`` exception on first access, which
        propagates up to the caller as a 502 from the orchestrator
        (matches the existing failure shape)."""
        if user_context is None:
            return self._server
        if user_context.is_admin:
            return self._server
        token = user_context.auth_token or ""
        if not token or token == self._admin_token:
            return self._server
        cached = self._per_user_servers.get(token)
        if cached is not None:
            return cached
        # plexapi's PlexServer constructor performs an /identity probe;
        # a bad token surfaces as a 401 here. We let it propagate so
        # the orchestrator surfaces it as DEST_WRITE_FAILED (or a
        # similar typed error if the caller wraps it).
        per_user = PlexServer(self._base_url, token, timeout=120)
        self._per_user_servers[token] = per_user
        return per_user

    def _search_for_filters(
        self,
        section,
        libtype: str,
        *,
        include_watched_only: bool,
        include_rated_only: bool,
    ) -> List:
        """Pick the right plexapi search shape per library type +
        filter combination. Mirrors the dispatch in
        ``snapshotter._bulk_fetch_for_filters`` + ``snapshot_watch_history``."""
        # Both filters off: full enumeration (rare; only used by the
        # ratings-only-but-want-all-shows path).
        if not include_watched_only and not include_rated_only:
            if libtype == "artist":
                return section.searchTracks()
            if libtype == "show":
                return section.searchEpisodes()
            return section.search()

        # Both filters on: single full fetch + caller filters.
        if include_watched_only and include_rated_only:
            if libtype == "artist":
                return [
                    t for t in section.searchTracks()
                    if (getattr(t, "viewCount", 0) or 0) > 0
                    or (getattr(t, "userRating", 0) or 0) > 0
                ]
            if libtype == "show":
                return [
                    ep for ep in section.searchEpisodes()
                    if (getattr(ep, "viewCount", 0) or 0) > 0
                    or (getattr(ep, "userRating", 0) or 0) > 0
                ]
            return [
                m for m in section.search()
                if (getattr(m, "viewCount", 0) or 0) > 0
                or (getattr(m, "userRating", 0) or 0) > 0
            ]

        # Single-filter paths: prefer server-side filter; fall back to
        # local filter on exception (mirrors snapshotter's existing
        # exception fallback).
        if include_watched_only:
            try:
                if libtype == "artist":
                    return section.searchTracks(viewCount__gt=0)
                if libtype == "show":
                    return section.searchEpisodes(viewCount__gt=0)
                return section.search(viewCount__gt=0)
            except Exception:
                if libtype == "artist":
                    return [t for t in section.searchTracks() if getattr(t, "viewCount", 0)]
                if libtype == "show":
                    return [ep for ep in section.searchEpisodes() if getattr(ep, "viewCount", 0)]
                return [m for m in section.search() if getattr(m, "viewCount", 0)]

        # include_rated_only
        try:
            if libtype == "artist":
                return section.searchTracks(userRating__gt=0)
            if libtype == "show":
                return section.searchEpisodes(userRating__gt=0)
            return section.search(userRating__gt=0)
        except Exception:
            if libtype == "artist":
                return [t for t in section.searchTracks() if getattr(t, "userRating", 0)]
            if libtype == "show":
                return [ep for ep in section.searchEpisodes() if getattr(ep, "userRating", 0)]
            return [m for m in section.search() if getattr(m, "userRating", 0)]


# ── Module-level helpers ────────────────────────────────────────────────────

# Plex item type -> entity-distinct MusicBrainz scheme. Jellyfin/Emby
# emit these schemes natively; the Plex adapter rewrites its generic
# ``musicbrainz://`` to the matching one so a track matches a track
# (and never an album or artist) when comparing GUIDs across backends.
_MB_SCHEME_BY_TYPE = {
    "track": "mbtrack",
    "album": "mbalbum",
    "artist": "mbartist",
}


def _rewrite_mb_scheme(guid: str, scheme: str) -> str:
    """Rewrite a generic ``musicbrainz://<id>`` GUID to
    ``<scheme>://<id>``. Any other GUID (``plex://``, ``local://``, an
    already entity-distinct ``mb*://``) is returned unchanged."""
    head, sep, rest = guid.partition("://")
    if sep and head == "musicbrainz":
        return f"{scheme}://{rest}"
    return guid


def _music_aware_guids(item: Any) -> Tuple[str, ...]:
    """Normalize a Plex item's GUIDs, then - for a music item - rewrite
    the generic ``musicbrainz://<id>`` scheme to the entity-distinct
    scheme (``mbtrack`` / ``mbalbum`` / ``mbartist``) keyed on the item
    type. ``guid_translator`` cannot do this: a bare MBID string does
    not say whether it is a recording, a release, or an artist id. The
    Plex adapter knows the item type, so the disambiguation happens
    here, leaving Plex and Jellyfin/Emby emitting the same schemes.

    Non-music items are returned with their GUIDs normalized only.
    """
    guids = normalize_guids(_all_guids(item))
    scheme = _MB_SCHEME_BY_TYPE.get(getattr(item, "type", "") or "")
    if not scheme:
        return tuple(guids)
    return tuple(_rewrite_mb_scheme(g, scheme) for g in guids)


def _plex_item_to_snapshot(plex_item, *, library_id: str) -> ItemSnapshot:
    """Convert a plexapi item to an ``ItemSnapshot``. Mirrors the field
    extraction in ``services/resolver.py:serialize_item`` but emits the
    typed dataclass instead of a JSON dict. The two consumers
    (snapshot.db writer and the in-memory engine) coexist; the JSON
    serializer can be re-derived from ``ItemSnapshot`` when the engine
    migration finishes."""
    last_viewed = getattr(plex_item, "lastViewedAt", None)
    last_viewed_epoch: Optional[float] = None
    if last_viewed is not None:
        try:
            last_viewed_epoch = last_viewed.timestamp()
        except Exception:
            last_viewed_epoch = None

    try:
        year = int(getattr(plex_item, "year", 0) or 0) or None
    except (TypeError, ValueError):
        year = None

    # addedAt parses to a datetime via plexapi; convert to epoch.
    added_epoch: Optional[float] = None
    added_raw = getattr(plex_item, "addedAt", None)
    if added_raw is not None:
        try:
            added_epoch = added_raw.timestamp()
        except Exception:
            added_epoch = None

    item_type = getattr(plex_item, "type", "") or ""
    # Hierarchy fields. plexapi exposes grandparentTitle + parentTitle
    # on episodes (show + season) and on tracks (artist + album).
    show_title = ""
    season_title = ""
    artist = ""
    album = ""
    # ADAPT-02: numeric hierarchy indices + parent GUID. Without these
    # the restore matcher loses its parent-GUID key and the
    # season/episode-number tiebreakers, degrading episode matching to
    # fuzzy title. serialize_item() in resolver.py captures the same
    # fields for the Plex-direct path, and the Jellyfin adapter captures
    # them too - the Plex adapter path was the lone gap.
    season_index: Optional[int] = None
    episode_index: Optional[int] = None
    grandparent_guid = ""
    if item_type == "episode":
        show_title = getattr(plex_item, "grandparentTitle", "") or ""
        season_title = getattr(plex_item, "parentTitle", "") or ""
        try:
            season_index = int(getattr(plex_item, "parentIndex", None))
        except (TypeError, ValueError):
            season_index = None
        try:
            episode_index = int(getattr(plex_item, "index", None))
        except (TypeError, ValueError):
            episode_index = None
        grandparent_guid = str(
            getattr(plex_item, "grandparentGuid", "") or ""
        )
    elif item_type == "track":
        artist = getattr(plex_item, "grandparentTitle", "") or ""
        album = getattr(plex_item, "parentTitle", "") or ""
        grandparent_guid = str(
            getattr(plex_item, "grandparentGuid", "") or ""
        )

    return ItemSnapshot(
        backend_item_id=str(getattr(plex_item, "ratingKey", "")),
        guids=tuple(_music_aware_guids(plex_item)),
        library_id=library_id,
        title=getattr(plex_item, "title", "") or "",
        type=item_type,
        year=year,
        file_path=_safe_file_path(plex_item) or None,
        view_count=int(getattr(plex_item, "viewCount", 0) or 0),
        last_viewed_at=last_viewed_epoch,
        view_offset_ms=int(getattr(plex_item, "viewOffset", 0) or 0),
        user_rating=(
            float(getattr(plex_item, "userRating", 0) or 0) or None
        ),
        # ADAPT-03: Plex has no per-user favorite concept - emit None
        # (the ItemSnapshot contract), NOT a fabricated False. A False
        # here triggers explicit un-favorite writes on a Plex ->
        # Jellyfin/Emby restore, clobbering destination users' favorites.
        is_favorite=None,
        added_at=added_epoch,
        show_title=show_title,
        season_title=season_title,
        artist=artist,
        album=album,
        season_index=season_index,
        episode_index=episode_index,
        grandparent_guid=grandparent_guid,
    )


__all__ = ["PlexAdapter"]
