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
from typing import Iterator, List, Optional

from plexapi.collection import Collection
from plexapi.playlist import Playlist
from plexapi.server import PlexServer

import services.state as state
from services.guid_translator import normalize_guids
from services.resolver import _all_guids, _disable_autoreload, _safe_file_path

from . import (
    CollectionSpec,
    ItemRef,
    ItemSnapshot,
    LibrarySpec,
    MediaServerAdapter,
    PlaylistSpec,
    ServerIdentity,
    UserContext,
    UserSpec,
    WriteResult,
)


log = logging.getLogger("plexmigrate.services.adapters.plex")


# Plex `:/scrobble` URL chunking for playlist + collection adds.
# Matches the existing chunk size used by ``services/restorer.py:
# _create_playlist_chunked`` (~8KB URL cap with comma-separated
# rating keys). Per-backend tunable so Jellyfin / Emby can pick their
# own threshold.
_PLEX_CONTAINER_CHUNK_SIZE = 100


class PlexAdapter(MediaServerAdapter):
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
        # Plan[PLAYLIST-MANAGEMENT] follow-up: per-user PlexServer
        # cache so a per-user-token write doesn't pay a fresh HTTP
        # handshake on every call. Keyed by the user's auth_token
        # (Plex's per-user X-Plex-Token). The admin server is held
        # separately on ``self._server`` and never enters this cache.
        self._per_user_servers: dict = {}
        # 2026-05-17 path-tail resolver index. Built lazily on first
        # ``resolve_by_path_tail`` call; cached per (adapter, depth)
        # for the lifetime of this adapter instance. See that method's
        # docstring for the matching rationale.
        self._path_tail_indexes: Dict[str, Dict[str, str]] = {}
        # 2026-05-17 username → local SystemAccount.id map. Built lazily
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

        Mirrors ``server/server_registry.py:get_server_users`` but
        returns the abstract ``UserSpec`` shape. Owner is detected
        either by ``SystemAccount.id == 1`` (Plex convention) or by
        name-match against ``myPlexAccount.username``."""
        out: List[UserSpec] = []
        owner_email = ""
        owner_username = ""
        try:
            account = self._server.myPlexAccount()
            owner_email = (getattr(account, "email", "") or "").strip()
            owner_username = (getattr(account, "username", "") or "").strip()
        except Exception as exc:
            log.debug("myPlexAccount unavailable: %s", exc)

        if owner_email:
            out.append(UserSpec(
                backend_user_id="",  # populated by the share-state refresh
                username=owner_email,
                display_name=owner_email,
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
            try:
                local_id = int(getattr(acct, "id", 0) or 0)
            except (TypeError, ValueError):
                local_id = 0
            if local_id == 1:
                continue
            if owner_username and name == owner_username:
                continue
            out.append(UserSpec(
                backend_user_id="",
                username=name,
                display_name=name,
                role="managed",
                is_admin=False,
            ))
        return out

    # ── Item resolution by GUID (restore-side cross-server matching) ──

    def resolve_by_guids(
        self,
        guids,
        *,
        library_id=None,
    ):
        """Look up the destination's ``backend_item_id`` (Plex
        ratingKey) by walking ``guids`` and trying
        ``server.library.getByGuid()`` on each.

        Plex's ``getByGuid`` accepts both the modern canonical form
        (``imdb://tt...``, ``tmdb://...``, ``tvdb://...``) and the
        legacy agent form (``com.plexapp.agents.imdb://tt...``). The
        engine has already normalised guids upstream via
        ``services.guid_translator.normalize_guids``; we try the
        normalised forms directly first, then fall back to the
        legacy agent strings so a Plex destination running an older
        agent version still resolves.

        Returns the destination's ratingKey as a string, or ``None``
        if no guid resolves. ``library_id`` narrows the search when
        supplied; absent or unmatched library_id falls through to a
        server-wide getByGuid (Plex's getByGuid is section-agnostic
        anyway)."""
        from plexapi.exceptions import NotFound, PlexApiException

        # Build the candidate guid list. Each input guid produces up
        # to two probes: its current form + the legacy agent form
        # (when applicable) so we work against both modern and
        # legacy Plex installs.
        candidates: List[str] = []
        for raw_guid in guids or ():
            if not raw_guid or "://" not in raw_guid:
                continue
            candidates.append(raw_guid)
            # Legacy-agent twin: try common scheme reversals so a
            # destination that hasn't migrated metadata agents yet
            # still matches.
            legacy = _to_legacy_agent(raw_guid)
            if legacy and legacy != raw_guid:
                candidates.append(legacy)

        # Deduplicate while preserving order so the most-likely
        # match is tried first.
        seen: set = set()
        for guid in candidates:
            if guid in seen:
                continue
            seen.add(guid)
            try:
                item = self._server.library.getByGuid(guid)
            except (NotFound, PlexApiException):
                continue
            except Exception as exc:
                log.debug("resolve_by_guids: getByGuid(%r) raised %s", guid, exc)
                continue
            if item is None:
                continue
            rk = getattr(item, "ratingKey", None)
            if rk is None:
                continue
            return str(rk)
        return None

    def resolve_by_path_tail(
        self,
        file_path: str,
        *,
        tail_components: int = 3,
    ) -> Optional[str]:
        """Last-resort cross-server resolver: match by the last N
        components of the file path (default 3 → ``artist/album/song``).

        2026-05-17 (end user request): GUID-based resolution fails for
        music tracks that legitimately have no metadata GUIDs (local
        files, pre-tagging-pass uploads, etc.). The source + destination
        Plex libraries usually share the same on-disk structure beneath
        a different root (``D:\\Music\\...`` vs ``/mnt/plex/...``), so
        comparing the trailing path components is a reliable identity
        check that's root-agnostic.

        First call builds an in-memory path-tail index across every
        library section (lazy + cached for the adapter's lifetime). The
        index keys are the lowercased last-N path components joined by
        ``/``; the value is the dest's ratingKey. Subsequent calls are
        O(1) dict lookups. The cache is per-adapter-instance, which in
        practice is per-orchestrator-request; we don't try to keep it
        warm across requests.

        Returns the matching dest ratingKey as a string, or ``None``
        when no match exists. ``file_path`` empty short-circuits to None.
        """
        if not file_path:
            return None
        src_parts = _normalize_path_parts(file_path)
        if not src_parts:
            return None
        n = min(tail_components, len(src_parts))
        if n <= 0:
            return None
        src_key = "/".join(src_parts[-n:])
        # Lazy build the path-tail index. Cache key includes the
        # component depth so callers asking for different depths each
        # get their own index.
        cache_key = f"path-tail-{n}"
        index = self._path_tail_indexes.get(cache_key)
        if index is None:
            index = {}
            try:
                sections = list(self._server.library.sections())
            except Exception as exc:
                log.warning(
                    "resolve_by_path_tail: library.sections() failed: %s", exc,
                )
                self._path_tail_indexes[cache_key] = index
                return None
            for section in sections:
                try:
                    leaves = _section_leaf_items(section) or []
                except Exception:
                    continue
                for it in leaves:
                    try:
                        fp = _safe_file_path(it)
                        if not fp:
                            continue
                        parts = _normalize_path_parts(fp)
                        if len(parts) < n:
                            continue
                        key = "/".join(parts[-n:])
                        rk = getattr(it, "ratingKey", None)
                        if rk is None:
                            continue
                        # First write wins — same-path collisions are
                        # rare and the end user's source picked ONE
                        # specific item, so any deterministic choice is
                        # acceptable.
                        index.setdefault(key, str(rk))
                    except Exception:
                        continue
            self._path_tail_indexes[cache_key] = index
            log.info(
                "resolve_by_path_tail: built path-tail-%d index with "
                "%d entries across %d section(s)",
                n, len(index), len(sections),
            )
        return index.get(src_key)

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

    # ── Per-user watch / rating writes ─────────────────────────────────
    #
    # All three writes go through the direct HTTP path
    # (``services.state._session`` + ``/:/scrobble`` etc.) rather than
    # plexapi's higher-level methods. This matches what the existing
    # restorer does and inherits the same token-in-header behaviour
    # (M1: never put the token in the query string).

    def set_watched(
        self,
        item_ref: ItemRef,
        *,
        view_count: int,
        last_viewed_at: Optional[float],
        user_context: UserContext,
    ) -> WriteResult:
        """Plex has no "set view count to N" endpoint; ``:/scrobble``
        increments by 1. The adapter issues up to N calls capped by
        ``state.VIEWCOUNT_INCREMENT_CAP`` (matches existing
        restorer.set_watched-equivalent behaviour). The caller (engine)
        is responsible for choosing the right ``view_count`` per the
        merge strategy in use (``higher`` / ``sum``)."""
        cap = int(getattr(state, "VIEWCOUNT_INCREMENT_CAP", 999))
        increments = max(0, min(int(view_count), cap))
        if increments == 0:
            return WriteResult.ok("no increments needed (target count 0)")
        token = user_context.auth_token or self._admin_token
        url = f"{self._base_url}/:/scrobble"
        try:
            for _ in range(increments):
                state._session.get(
                    url,
                    params={
                        "key": item_ref.backend_item_id,
                        "identifier": "com.plexapp.plugins.library",
                    },
                    headers={"X-Plex-Token": token},
                    timeout=5,
                )
        except Exception as exc:
            return WriteResult.fail(f"scrobble failed: {exc}")
        return WriteResult.ok(f"incremented {increments} time(s)")

    def set_resume_position(
        self,
        item_ref: ItemRef,
        offset_ms: int,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        token = user_context.auth_token or self._admin_token
        url = f"{self._base_url}/:/progress"
        try:
            state._session.get(
                url,
                params={
                    "key": item_ref.backend_item_id,
                    "identifier": "com.plexapp.plugins.library",
                    "time": int(offset_ms),
                    "state": "stopped",
                    "hasMDE": 1,
                },
                headers={"X-Plex-Token": token},
                timeout=5,
            )
        except Exception as exc:
            return WriteResult.fail(f"progress failed: {exc}")
        return WriteResult.ok()

    def set_rating(
        self,
        item_ref: ItemRef,
        rating: float,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        token = user_context.auth_token or self._admin_token
        url = f"{self._base_url}/:/rate"
        try:
            state._session.put(
                url,
                params={
                    "key": item_ref.backend_item_id,
                    "identifier": "com.plexapp.plugins.library",
                    "rating": float(rating),
                },
                headers={"X-Plex-Token": token},
                timeout=10,
            )
        except Exception as exc:
            return WriteResult.fail(f"rate failed: {exc}")
        return WriteResult.ok()

    # set_favorite stays at the ABC default (unsupported).

    # ── Playlists ──────────────────────────────────────────────────────

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

        2026-05-17 bug fix (operator report — managed users showing
        owner's playlists):
        ``PlexServer.playlists()`` returns playlists visible to the
        authenticated token. When the auth fell back to the ADMIN
        token (per_user_token mode + no saved per-user token OR plain
        owner_token mode) and the target user is a managed user, the
        bare call returned the OWNER's playlists, which we then cached
        under the managed user's key — visible cross-user contamination.

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
        out: List[PlaylistSpec] = []
        for pl in playlists:
            try:
                items_tuple = tuple(
                    ItemRef(
                        backend_item_id=str(getattr(it, "ratingKey", "")),
                        guids=tuple(normalize_guids(_all_guids(it))),
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
            out.append(PlaylistSpec(
                playlist_id=str(getattr(pl, "ratingKey", "")),
                name=getattr(pl, "title", "") or "",
                is_smart=bool(getattr(pl, "smart", False)),
                smart_filter_json=smart_filter,
                items=items_tuple,
            ))
        return out

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

        2026-05-17: also surfaces ``file_path`` on each ItemRef so the
        orchestrator's path-tail fallback can resolve cross-server
        copies when GUIDs don't match (common for music tracks)."""
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
                    guids=tuple(normalize_guids(_all_guids(it))),
                    title=getattr(it, "title", "") or "",
                    file_path=_safe_file_path(it),
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
                            guids=tuple(normalize_guids(_all_guids(it))),
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


# ── Module-level helpers ────────────────────────────────────────────────────

_CANONICAL_TO_LEGACY_AGENT = {
    "imdb": "com.plexapp.agents.imdb",
    "tmdb": "com.plexapp.agents.themoviedb",
    "tvdb": "com.plexapp.agents.thetvdb",
    "musicbrainz": "com.plexapp.agents.musicbrainz",
}


def _to_legacy_agent(canonical_guid: str) -> Optional[str]:
    """Reverse of ``services/guid_translator.normalize_guids`` for the
    handful of Plex agents that have legacy forms. Used by
    :meth:`PlexAdapter.resolve_by_guids` to probe a destination that
    hasn't migrated its metadata to the modern agent set.

    Returns ``None`` when there's no known legacy mapping (passes
    through unchanged for ``plex://`` / ``mbtrack://`` etc.)."""
    scheme, _, value = canonical_guid.partition("://")
    if not scheme or not value:
        return None
    legacy_scheme = _CANONICAL_TO_LEGACY_AGENT.get(scheme.lower())
    if not legacy_scheme:
        return None
    return f"{legacy_scheme}://{value}"


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
    if item_type == "episode":
        show_title = getattr(plex_item, "grandparentTitle", "") or ""
        season_title = getattr(plex_item, "parentTitle", "") or ""
    elif item_type == "track":
        artist = getattr(plex_item, "grandparentTitle", "") or ""
        album = getattr(plex_item, "parentTitle", "") or ""

    return ItemSnapshot(
        backend_item_id=str(getattr(plex_item, "ratingKey", "")),
        guids=tuple(normalize_guids(_all_guids(plex_item))),
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
        is_favorite=False,  # Plex has no per-user favorite
        added_at=added_epoch,
        show_title=show_title,
        season_title=season_title,
        artist=artist,
        album=album,
    )


__all__ = ["PlexAdapter"]
