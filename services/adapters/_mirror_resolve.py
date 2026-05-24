"""Shared mirror-first resolution for media-server adapters.

The per-server metadata mirror (server_mirror.db) caches a server's
item universe so cross-server resolution answers from local SQLite
instead of a live API walk. PlexAdapter pioneered the mirror-first
resolve wrappers; this mixin lifts them into one place so
JellyfinAdapter / EmbyAdapter reuse the implementation rather than
forking it.

Every mirror row is keyed by the app's own registry server UID
(``server_registry.make_server_id()`` produces
``<service_type>_<uuid4hex>``), never a backend-native id like the
Plex ``machineIdentifier``. Keying on the app UID keeps the mirror
backend-agnostic and keeps backend-native identifiers out of logs. A
concrete adapter supplies that UID via ``_mirror_server_id``; the
rest of the resolve + cold-start logic here is backend-agnostic.
"""

from __future__ import annotations

import logging
from typing import List, Optional


log = logging.getLogger("plexmigrate.services.adapters.mirror_resolve")


class MirrorResolveMixin:
    """Mirror-first resolve helpers shared by every adapter.

    A concrete adapter must provide:
      * ``_mirror_server_id()`` - the app registry UID for this
        server (the mirror's row key).
      * ``iter_sections_for_mirror()`` + ``iter_section_items_for_mirror()``
        - the provider methods the cold-start sync drives.
      * a ``backend`` attribute ('plex' | 'jellyfin' | 'emby').
    """

    # Concrete adapters set their own value; declared here so the
    # mixin's cold-start sync can read it without an AttributeError.
    backend: str = ""

    def _mirror_server_id(self) -> str:
        """The app registry UID this adapter's mirror rows are keyed
        by. The default ``""`` makes every mirror path below inert;
        concrete adapters override it."""
        return ""

    def _mirror_db_ready(self) -> bool:
        """True iff the mirror DB is initialised for this process.
        Tests and ad-hoc adapter use construct adapters without
        booting the mirror DB; every mirror path short-circuits when
        it is absent so those callers keep working unchanged."""
        try:
            from server import server_mirror_db
            server_mirror_db.get_connection()
            return True
        except (RuntimeError, ImportError):
            return False

    def _mirror_usable_mode(self) -> Optional[str]:
        """The effective mirror mode for this server, or ``None`` when
        the mirror cannot be consulted at all (no DB, no server UID).
        A return of ``"always-live"`` means the operator has disabled
        mirror reads for this server."""
        if not self._mirror_db_ready():
            return None
        server_id = self._mirror_server_id()
        if not server_id:
            return None
        try:
            from services import server_mirror
            return server_mirror.effective_mode_for(server_id)
        except Exception:
            return None

    def _ensure_mirror_warmed_for(
        self,
        item_type_hint: str = "",
        library_id: Optional[str] = None,
    ) -> None:
        """Trigger a cold-start mirror sync for this server when the
        mirror has no items yet. Single-flight via the sync layer's
        per-(server, section) build event so concurrent resolver
        calls do not duplicate work.

        Mode handling:
          * auto: always trigger the cold-start sync when empty.
          * always-live + engine_mirror_always_live_writethrough:
            still sync so an operator flip back to auto is instant.
          * always-live + write-through off: no sync (true bypass).

        Best-effort: any failure is logged + swallowed. Resolution
        falls through to the live path on a mirror miss anyway."""
        if not self._mirror_db_ready():
            return
        from services import server_mirror, tunables
        server_id = self._mirror_server_id()
        if not server_id:
            return
        try:
            mode = server_mirror.effective_mode_for(server_id)
        except Exception:
            return
        if mode == "always-live":
            try:
                if not tunables.engine_mirror_always_live_writethrough():
                    return
            except Exception:
                return
        try:
            existing = server_mirror.count_items(server_id=server_id)
        except Exception:
            return
        if existing > 0:
            return
        try:
            sections = self.iter_sections_for_mirror()
            if library_id:
                sections = [
                    s for s in sections
                    if s.section_id == str(library_id)
                ]
            elif item_type_hint:
                wanted = {
                    "movie": {"movie"},
                    "episode": {"show"},
                    "show": {"show"},
                    "season": {"show"},
                    "track": {"artist"},
                    "album": {"artist"},
                    "artist": {"artist"},
                    "photo": {"photo"},
                }.get(item_type_hint.lower())
                if wanted:
                    sections = [
                        s for s in sections
                        if (s.section_type or "").lower() in wanted
                    ]
            if not sections:
                return
            server_mirror.upsert_server_state(
                server_id=server_id,
                backend=getattr(self, "backend", "") or "",
            )
            log.info(
                "%s._ensure_mirror_warmed_for: cold-start mirror sync "
                "server=%s scopes=%d (hint=%r)",
                type(self).__name__, server_id, len(sections),
                item_type_hint or "all",
            )
            server_mirror.sync_for_job(
                server_id=server_id,
                sections=sections,
                item_provider=self.iter_section_items_for_mirror,
            )
        except Exception as exc:
            log.warning(
                "%s._ensure_mirror_warmed_for: sync failed for "
                "server=%s: %s", type(self).__name__, server_id, exc,
            )

    def _mirror_lookup_guids(
        self,
        guids: List[str],
        library_id: Optional[str] = None,
    ) -> Optional[str]:
        """Mirror-first GUID resolve. Returns a rating_key or None."""
        if self._mirror_usable_mode() in (None, "always-live"):
            return None
        try:
            from services import server_mirror
            return server_mirror.lookup_by_guids(
                server_id=self._mirror_server_id(), guids=guids,
                section_id=library_id,
            )
        except Exception as exc:
            log.debug("_mirror_lookup_guids failed: %s", exc)
            return None

    def _mirror_lookup_full_path(
        self,
        file_path: str,
        item_type_hint: Optional[str] = None,
    ) -> Optional[str]:
        """Mirror-first full-path resolve. Returns a rating_key or None."""
        if self._mirror_usable_mode() in (None, "always-live"):
            return None
        try:
            from services import server_mirror
            return server_mirror.lookup_by_full_path(
                server_id=self._mirror_server_id(), file_path=file_path,
                item_type_hint=item_type_hint or None,
            )
        except Exception as exc:
            log.debug("_mirror_lookup_full_path failed: %s", exc)
            return None

    def _mirror_lookup_path_tail(
        self,
        file_path: str,
        tail_components: int = 3,
        item_type_hint: Optional[str] = None,
    ) -> Optional[str]:
        """Mirror-first path-tail resolve. Returns a rating_key or None."""
        if self._mirror_usable_mode() in (None, "always-live"):
            return None
        try:
            from services import server_mirror
            return server_mirror.lookup_by_path_tail(
                server_id=self._mirror_server_id(), file_path=file_path,
                tail_components=tail_components,
                item_type_hint=item_type_hint or None,
            )
        except Exception as exc:
            log.debug("_mirror_lookup_path_tail failed: %s", exc)
            return None

    def _mirror_lookup_hierarchy(
        self,
        *,
        item_type: str,
        title: str,
        grandparent_guid: Optional[str] = None,
        show_title: Optional[str] = None,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
    ) -> Optional[str]:
        """Mirror-first hierarchy resolve (parent GUID / show / artist
        + leaf coordinates). Returns a single rating_key on a unique
        match, or None."""
        if self._mirror_usable_mode() in (None, "always-live"):
            return None
        try:
            from services import server_mirror
            return server_mirror.lookup_by_hierarchy(
                server_id=self._mirror_server_id(),
                item_type=item_type, title=title,
                grandparent_guid=grandparent_guid,
                show_title=show_title,
                season_number=season_number,
                episode_number=episode_number,
                artist=artist, album=album,
            )
        except Exception as exc:
            log.debug("_mirror_lookup_hierarchy failed: %s", exc)
            return None

    def _mirror_lookup_fuzzy_title(
        self,
        title: str,
        *,
        item_type: str,
        artist: Optional[str] = None,
        show_title: Optional[str] = None,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        album: Optional[str] = None,
        ambiguous_behavior: str = "strict",
    ) -> Optional[List[str]]:
        """Mirror-first fuzzy-title resolve. Returns a list of
        rating_keys (same shape as resolve_by_fuzzy_title), or None on
        miss. The ambiguous_behavior policy is applied in Python on
        top of the mirror's candidate set."""
        if self._mirror_usable_mode() in (None, "always-live"):
            return None
        try:
            from services import server_mirror
            candidates = server_mirror.lookup_by_fuzzy_title(
                server_id=self._mirror_server_id(), title=title,
                item_type=item_type, artist=artist,
                show_title=show_title, season_number=season_number,
                episode_number=episode_number, album=album,
                ambiguous_behavior=ambiguous_behavior,
            )
        except Exception as exc:
            log.debug("_mirror_lookup_fuzzy_title failed: %s", exc)
            return None
        if not candidates:
            return None
        if len(candidates) == 1:
            return [str(candidates[0]["rating_key"])]
        mode = (ambiguous_behavior or "strict").lower()
        if mode == "first":
            return [str(candidates[0]["rating_key"])]
        if mode == "all":
            return [str(c["rating_key"]) for c in candidates]
        # strict: refuse to guess.
        return None
