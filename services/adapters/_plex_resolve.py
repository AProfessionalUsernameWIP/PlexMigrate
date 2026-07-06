"""Plex item-resolution mixin.

The four-tier cross-server item-resolution machinery the engine
calls during a restore:

  1. ``resolve_by_guids``       — GUID-indexed lookup with per-(section, scheme)
                                  blacklist short-circuit.
  2. ``resolve_by_full_path``   — exact file-path match.
  3. ``resolve_by_path_tail``   — last-N path-components match (root-agnostic).
  4. ``resolve_by_fuzzy_title`` — title + hierarchy + tiebreaker fallback.

All four tiers consult the per-server metadata mirror FIRST (via the
:class:`MirrorResolveMixin` helpers) and fall through to live plexapi
walks on a mirror miss. Private helpers that support these methods
(``_sections_for_item_type``, ``_build_path_indexes``,
``_build_artist_track_index``, the per-(section, scheme) blacklist
counters, and the ``clear_*_for_tests`` hooks) live alongside them
here.

Mixed into ``PlexAdapter`` — all state (``self._cache_lock``,
``self._sections_cache``, ``self._library_of_truth``,
``self._path_tail_indexes``, ``self._index_build_events``,
``self._artist_track_indexes``, ``self._guid_attempt_state``,
``self._machine_id_cached``) lives on the adapter; the mixin only
contributes methods that read / mutate it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from services.translation.guid_translator import (
    _mb_searchable_guids,
    _to_legacy_agent,
)
from services.resolver import (
    _normalize_path_parts,
    _safe_file_path,
    _section_leaf_items,
)


log = logging.getLogger("plexmigrate.services.adapters.plex")


# After this many consecutive misses on the same (section,
# guid-scheme) combination, stop probing it. Plex music sections
# never resolve mbid:// GUIDs via section.search + filters={"guid":
# ...} so every probe wastes ~3s; after 3 misses it is hopeless and
# we short-circuit.
_GUID_BLACKLIST_THRESHOLD = 3


class PlexResolveMixin:
    """Item-resolution methods for :class:`PlexAdapter`.

    Owns the 4-tier resolver suite + their private helpers + the
    per-test reset hooks. Depends on ``MirrorResolveMixin`` methods
    (``_ensure_mirror_warmed_for``, ``_mirror_lookup_*``) being
    available on the same MRO."""

    def _sections_for_item_type(
        self,
        item_type: str,
        *,
        library_id: Optional[str] = None,
    ) -> List[Any]:
        """Return the destination's library sections scoped to the
        ones that could hold an item of ``item_type``.

        Plex section types map to ItemRef item_types:
          * ``"movie"``  → ``movie`` sections
          * ``"episode"``/``"show"``/``"season"`` → ``show`` sections
          * ``"track"``/``"album"``/``"artist"`` → ``artist`` sections
          * ``"photo"``  → ``photo`` sections
          * unknown / empty → every section (no scoping)

        Without this filter, every resolver walks every section,
        hitting Photos / Movies / Music / etc. for every music track.
        With the hint, a music playlist's items only probe music
        sections. The library_id override still wins when supplied
        (caller scoped a specific section).
        """
        # Cache the raw sections list once per adapter lifetime.
        # Without it, ``library.sections()`` (a ~200ms HTTP call) runs
        # on every per-item resolver call - tens of seconds wasted on
        # the same fetch across a batch.
        with self._cache_lock:
            sections_cached = self._sections_cache
        if sections_cached is None:
            try:
                sections_cached = list(self._server.library.sections())
            except Exception as exc:
                log.warning(
                    "_sections_for_item_type: library.sections() failed: %s",
                    exc,
                )
                sections_cached = []
            with self._cache_lock:
                # First write wins under racing concurrent callers.
                if self._sections_cache is None:
                    self._sections_cache = sections_cached
                else:
                    sections_cached = self._sections_cache
        sections = list(sections_cached)
        if not sections:
            return []
        # library_id wins when supplied (caller knows the section).
        if library_id:
            try:
                scoped = [
                    s for s in sections
                    if str(getattr(s, "key", "") or "") == str(library_id)
                ]
                if scoped:
                    return scoped
            except Exception:
                pass
        # Map ItemRef item_type -> matching Plex section types.
        wanted: Optional[set] = None
        t = (item_type or "").lower()
        if t == "movie":
            wanted = {"movie"}
        elif t in ("episode", "show", "season"):
            wanted = {"show"}
        elif t in ("track", "album", "artist"):
            wanted = {"artist"}
        elif t == "photo":
            wanted = {"photo"}
        if wanted is None:
            return sections
        scoped = [
            s for s in sections
            if (getattr(s, "type", "") or "").lower() in wanted
        ]
        # Library-of-truth: when a prior call resolved a real match
        # against ONE of the matching sections, narrow future calls
        # to that same section. Operator-reported case: dest has
        # both "Kai's Audio Files" + "Music" sections; first track
        # resolved to "Music"; every subsequent track of the same
        # type should skip "Kai's Audio Files" entirely.
        key = (item_type or "").lower()
        truth_key = self._library_of_truth.get(key)
        if truth_key and scoped:
            anchored = [
                s for s in scoped
                if str(getattr(s, "key", "") or "") == truth_key
            ]
            if anchored:
                return anchored
        # If the hint produced no matches (e.g. dest server has no
        # music sections at all), fall back to the full list so the
        # resolver still has something to try. Better to do extra
        # work than to silently miss.
        return scoped or sections

    def _record_library_of_truth(
        self, item_type_hint: str, section: Any,
    ) -> None:
        """Remember which destination section gave the first real
        match for this item_type. Called by every resolver right
        after a successful hit. Idempotent: the first write wins
        so a section that's been resolving consistently stays
        locked in."""
        key = (item_type_hint or "").lower()
        if not key:
            return
        sec_key = str(getattr(section, "key", "") or "")
        if not sec_key:
            return
        with self._cache_lock:
            if key in self._library_of_truth:
                return
            self._library_of_truth[key] = sec_key
        log.info(
            "library_of_truth: locked %r -> section %r (key=%s); "
            "future items of this type only probe this section.",
            key, getattr(section, "title", "?"), sec_key,
        )

    def clear_library_of_truth_for_tests(self) -> None:
        """Test-only helper to drop the cached truth so each test
        starts fresh."""
        self._library_of_truth.clear()

    @staticmethod
    def _guid_scheme(guid: str) -> str:
        """Thin shim: the canonical implementation lives in the shared
        translator so the legacy-prefix detection cannot diverge from
        normalize_guid's view of what counts as a legacy agent form."""
        from services.translation.guid_translator import scheme_for_blacklist_key
        return scheme_for_blacklist_key(guid)

    def _guid_attempt_record(
        self,
        section_key: str,
        scheme: str,
        *,
        hit: bool,
    ) -> None:
        """Update the per-(section, scheme) attempt counter. Called
        after every section.search probe in resolve_by_guids."""
        if not section_key or not scheme:
            return
        key = (section_key, scheme)
        with self._cache_lock:
            state = self._guid_attempt_state.setdefault(
                key, {"attempts": 0, "hits": 0},
            )
            state["attempts"] += 1
            if hit:
                state["hits"] += 1

    def _guid_attempt_blacklisted(
        self, section_key: str, scheme: str,
    ) -> bool:
        """True when this (section, scheme) has accumulated
        ``_GUID_BLACKLIST_THRESHOLD`` misses with zero hits — at
        which point we stop probing it to save ~3s per item."""
        if not section_key or not scheme:
            return False
        # ADAPT-09: read under the same lock as the writer
        # (_guid_attempt_record). Keep the critical section minimal.
        with self._cache_lock:
            state = self._guid_attempt_state.get((section_key, scheme))
            if state is None:
                return False
            return (
                state.get("hits", 0) == 0
                and state.get("attempts", 0) >= _GUID_BLACKLIST_THRESHOLD
            )

    def clear_guid_attempt_state_for_tests(self) -> None:
        """Test-only helper to drop the per-(section, scheme) GUID
        attempt counters so each test starts fresh."""
        self._guid_attempt_state.clear()

    def clear_sections_cache_for_tests(self) -> None:
        """Test-only helper to drop the cached
        ``library.sections()`` result."""
        with self._cache_lock:
            self._sections_cache = None

    def clear_index_build_events_for_tests(self) -> None:
        """Test-only helper to drop the per-key build-in-progress
        events so each test starts fresh."""
        with self._cache_lock:
            self._index_build_events.clear()

    def resolve_by_guids(
        self,
        guids,
        *,
        library_id=None,
        item_type_hint="",
    ):
        """Look up the destination's ``backend_item_id`` (Plex
        ratingKey) by walking ``guids`` and trying
        ``server.library.getByGuid()`` on each.

        Plex's ``getByGuid`` accepts both the modern canonical form
        (``imdb://tt...``, ``tmdb://...``, ``tvdb://...``) and the
        legacy agent form (``com.plexapp.agents.imdb://tt...``). The
        engine has already normalised guids upstream via
        ``services.translation.guid_translator.normalize_guids``; we try the
        normalised forms directly first, then fall back to the
        legacy agent strings so a Plex destination running an older
        agent version still resolves.

        Returns the destination's ratingKey as a string, or ``None``
        if no guid resolves. ``library_id`` narrows the search when
        supplied; absent or unmatched library_id scopes the search
        across every section.

        plexapi 4.x's ``Library`` class has no ``getByGuid`` method.
        ``LibrarySection.getGuid()`` exists but is documented as
        "Only available for the Plex Movie and Plex TV Series
        agents" - useless for music's ``mbid://`` GUIDs.

        The fix: use ``section.search(filters={"guid": guid})``
        per section. This is a generic Plex server-side metadata
        filter that works for ANY agent (Movie / TV / Music /
        Photo). plexapi forwards the filter as
        ``/library/sections/<id>/all?guid=<urlencoded>`` and Plex
        matches against its internal GUID index. Tried first; if
        the filter approach fails (older Plex versions may not
        support it), fall back to ``section.getGuid()`` per section
        for movie/TV agents that DO support it."""
        from plexapi.exceptions import NotFound, PlexApiException

        # Build the candidate guid list. Each input guid produces its
        # current form + the legacy agent form (when applicable) so we
        # work against both modern and legacy Plex installs. An
        # entity-distinct MusicBrainz guid (mbtrack / mbalbum /
        # mbartist) from a cross-backend Jellyfin/Emby snapshot
        # additionally produces the generic ``musicbrainz://`` +
        # legacy agent forms a Plex destination indexes.
        candidates: List[str] = []
        for raw_guid in guids or ():
            if not raw_guid or "://" not in raw_guid:
                continue
            candidates.append(raw_guid)
            legacy = _to_legacy_agent(raw_guid)
            if legacy and legacy != raw_guid:
                candidates.append(legacy)
            candidates.extend(_mb_searchable_guids(raw_guid))

        # Deduplicate while preserving order so the most-likely
        # match is tried first.
        seen: set = set()
        unique_guids: List[str] = []
        for guid in candidates:
            if guid in seen:
                continue
            seen.add(guid)
            unique_guids.append(guid)

        if not unique_guids:
            return None

        # Mirror-first lookup.
        # Cold-start a sync if the mirror is empty for this server's
        # relevant scope, then query the mirror. On hit, skip the
        # live walk entirely. On miss, fall through to the existing
        # live path below.
        self._ensure_mirror_warmed_for(
            item_type_hint=item_type_hint, library_id=library_id,
        )
        mirror_hit = self._mirror_lookup_guids(
            unique_guids, library_id=library_id,
        )
        if mirror_hit is not None:
            log.debug(
                "resolve_by_guids: mirror hit -> %s "
                "(skipping live walk)", mirror_hit,
            )
            return mirror_hit

        # Scope to sections that can
        # hold the source item's type so a music GUID lookup doesn't
        # probe Photos / Movies / Home Movies sections.
        sections = self._sections_for_item_type(
            item_type_hint, library_id=library_id,
        )
        if not sections:
            return None

        # Primary path: per-section server-side filter. Works for
        # ALL agents (music's mbid://, movies' imdb://, etc.).
        # After 3 consecutive (section, scheme) misses with zero hits,
        # blacklist that combo. Plex music sections never resolve
        # mbid:// via this path, so every miss wastes ~3s; the
        # short-circuit saves that cost per subsequent item in the
        # batch.
        for guid in unique_guids:
            scheme = self._guid_scheme(guid)
            for section in sections:
                section_key = str(getattr(section, "key", "") or "")
                if self._guid_attempt_blacklisted(section_key, scheme):
                    continue
                try:
                    results = section.search(filters={"guid": guid})
                except (NotFound, PlexApiException):
                    self._guid_attempt_record(
                        section_key, scheme, hit=False,
                    )
                    continue
                except Exception as exc:
                    log.debug(
                        "resolve_by_guids: section.search(filters=guid) "
                        "failed on %r for guid %r: %s",
                        getattr(section, "title", "?"), guid, exc,
                    )
                    self._guid_attempt_record(
                        section_key, scheme, hit=False,
                    )
                    continue
                results_list = list(results or [])
                # No hits — record the miss for blacklisting.
                if not results_list:
                    self._guid_attempt_record(
                        section_key, scheme, hit=False,
                    )
                    # Log when we just crossed the threshold so the
                    # operator can see WHY future items skip this combo.
                    # ADAPT-09: read under the same lock as the writer
                    # (_guid_attempt_record); keep the section minimal.
                    with self._cache_lock:
                        state = self._guid_attempt_state.get(
                            (section_key, scheme), {},
                        )
                        crossed_threshold = (
                            state.get("hits", 0) == 0
                            and state.get("attempts", 0)
                            == _GUID_BLACKLIST_THRESHOLD
                        )
                    if crossed_threshold:
                        log.info(
                            "resolve_by_guids: blacklisting "
                            "(section=%r, scheme=%r) after %d misses "
                            "with 0 hits — future items skip this combo "
                            "to save ~3s per probe.",
                            getattr(section, "title", "?"),
                            scheme, _GUID_BLACKLIST_THRESHOLD,
                        )
                    continue
                for r in results_list:
                    rk = getattr(r, "ratingKey", None)
                    if rk is not None:
                        log.debug(
                            "resolve_by_guids: filter hit %r -> %s in "
                            "section %r", guid, rk,
                            getattr(section, "title", "?"),
                        )
                        self._guid_attempt_record(
                            section_key, scheme, hit=True,
                        )
                        self._record_library_of_truth(
                            item_type_hint, section,
                        )
                        return str(rk)

        # Fallback: per-section getGuid() for the Movie/TV agents
        # that support it. Music sections raise NotImplementedError
        # here and are skipped.
        #
        # Apply the same blacklist as the primary path. If a (section,
        # scheme) has missed 3 times on the PRIMARY path, the fallback
        # would just waste another network call - skip both. Misses
        # are recorded against the shared counter so a fallback that's
        # the only path can still accumulate to the threshold.
        for guid in unique_guids:
            scheme = self._guid_scheme(guid)
            for section in sections:
                section_key = str(getattr(section, "key", "") or "")
                if self._guid_attempt_blacklisted(section_key, scheme):
                    continue
                _getter = getattr(section, "getGuid", None)
                if not callable(_getter):
                    continue
                try:
                    item = _getter(guid)
                except (NotFound, PlexApiException, NotImplementedError):
                    self._guid_attempt_record(
                        section_key, scheme, hit=False,
                    )
                    continue
                except Exception as exc:
                    log.debug(
                        "resolve_by_guids: section.getGuid(%r) raised "
                        "%s on section %r",
                        guid, exc, getattr(section, "title", "?"),
                    )
                    self._guid_attempt_record(
                        section_key, scheme, hit=False,
                    )
                    continue
                if item is None:
                    self._guid_attempt_record(
                        section_key, scheme, hit=False,
                    )
                    continue
                rk = getattr(item, "ratingKey", None)
                if rk is not None:
                    log.debug(
                        "resolve_by_guids: getGuid fallback hit %r -> %s "
                        "in section %r", guid, rk,
                        getattr(section, "title", "?"),
                    )
                    self._guid_attempt_record(
                        section_key, scheme, hit=True,
                    )
                    self._record_library_of_truth(item_type_hint, section)
                    return str(rk)
        return None

    def resolve_by_path_tail(
        self,
        file_path: str,
        *,
        tail_components: int = 3,
        item_type_hint: str = "",
        library_id: Optional[str] = None,
    ) -> Optional[str]:
        """Last-resort cross-server resolver: match by the last N
        components of the file path (default 3 → ``artist/album/song``).

        GUID-based resolution fails for music tracks that legitimately
        have no metadata GUIDs (local files, pre-tagging-pass uploads,
        etc.). The source + destination Plex libraries usually share
        the same on-disk structure beneath a different root
        (``D:\\Music\\...`` vs ``/mnt/plex/...``), so comparing the
        trailing path components is a reliable identity check that's
        root-agnostic.

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
        # Mirror-first lookup.
        self._ensure_mirror_warmed_for(
            item_type_hint=item_type_hint, library_id=library_id,
        )
        mirror_hit = self._mirror_lookup_path_tail(
            file_path, tail_components=n,
            item_type_hint=item_type_hint,
        )
        if mirror_hit is not None:
            log.debug(
                "resolve_by_path_tail: mirror hit -> %s "
                "(skipping live walk)", mirror_hit,
            )
            return mirror_hit
        src_key = "/".join(src_parts[-n:])
        # Share the walk with resolve_by_full_path so the section
        # iteration happens ONCE per scope rather than once per
        # resolver. The shared builder populates both indexes
        # simultaneously.
        _, tail_index = self._build_path_indexes(
            item_type_hint, tail_components=n, library_id=library_id,
        )
        return tail_index.get(src_key)

    def _build_path_indexes(
        self,
        item_type_hint: str,
        tail_components: int,
        library_id: Optional[str] = None,
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Build BOTH the full-path index and the path-tail-N index
        in a single section walk. Doing it in one pass rather than
        two separate walks of the same (large) section set halves the
        up-front cost and makes it more predictable.

        Returns ``(full_path_index, path_tail_index)`` - both keyed
        by their respective string keys and cached on the adapter
        instance under the same cache-key family so subsequent calls
        return the cached pair.

        Thread-safe via ``self._cache_lock`` + per-key build events:
        when multiple workers race on the first call, only ONE walks
        the sections + builds the index; the others wait on the build
        event and then read the populated cache. Without the build
        event, every worker would race through the cache check and
        rebuild the index redundantly."""
        cache_key_full = f"full-path-{(item_type_hint or 'all').lower()}"
        cache_key_tail = (
            f"path-tail-{tail_components}-"
            f"{(item_type_hint or 'all').lower()}"
        )
        # Composite key for the build-in-progress event so the
        # full-path + path-tail builds for one scope are
        # coordinated as a single unit.
        build_key = f"{cache_key_full}|{cache_key_tail}"

        with self._cache_lock:
            full_index = self._path_tail_indexes.get(cache_key_full)
            tail_index = self._path_tail_indexes.get(cache_key_tail)
            if full_index is not None and tail_index is not None:
                return full_index, tail_index
            event = self._index_build_events.get(build_key)
            if event is None:
                # WE are the builder. Publish the event so other
                # threads know to wait on it.
                event = threading.Event()
                self._index_build_events[build_key] = event
                we_build = True
            else:
                we_build = False

        if not we_build:
            # Another thread is building. Wait for it to finish,
            # then read the cache it published.
            event.wait()
            with self._cache_lock:
                full_index = self._path_tail_indexes.get(
                    cache_key_full, {},
                )
                tail_index = self._path_tail_indexes.get(
                    cache_key_tail, {},
                )
            return full_index, tail_index

        # WE build. Always release the event on exit so a build
        # failure doesn't strand the waiters forever.
        try:
            # Try the persisted on-disk cache before walking Plex's
            # library. If a fresh-enough row exists, populate the
            # in-memory caches from it and skip the ~32-second walk
            # entirely.
            scope_tag = (item_type_hint or "all").lower()
            persisted = self._try_load_persisted_path_index(
                scope_tag, tail_components,
            )
            if persisted is not None:
                full_index, tail_index = persisted
                with self._cache_lock:
                    self._path_tail_indexes[cache_key_full] = full_index
                    self._path_tail_indexes[cache_key_tail] = tail_index
                return full_index, tail_index

            full_index = {}
            tail_index = {}
            sections = self._sections_for_item_type(
                item_type_hint, library_id=library_id,
            )
            if not sections:
                with self._cache_lock:
                    self._path_tail_indexes[cache_key_full] = full_index
                    self._path_tail_indexes[cache_key_tail] = tail_index
                return full_index, tail_index
            started = time.perf_counter()
            total_walked = 0
            for section in sections:
                try:
                    leaves = _section_leaf_items(section) or []
                except Exception:
                    continue
                for it in leaves:
                    total_walked += 1
                    try:
                        fp = _safe_file_path(it)
                        if not fp:
                            continue
                        rk = getattr(it, "ratingKey", None)
                        if rk is None:
                            continue
                        rk_s = str(rk)
                        full_index.setdefault(fp, rk_s)
                        parts = _normalize_path_parts(fp)
                        if len(parts) >= tail_components:
                            tail_key = "/".join(parts[-tail_components:])
                            tail_index.setdefault(tail_key, rk_s)
                    except Exception:
                        continue
            with self._cache_lock:
                self._path_tail_indexes[cache_key_full] = full_index
                self._path_tail_indexes[cache_key_tail] = tail_index
            elapsed = time.perf_counter() - started
            log.info(
                "_build_path_indexes: walked %d items in %.1fs -> full=%d "
                "tail=%d entries (scope=%s, sections=%d)",
                total_walked, elapsed, len(full_index), len(tail_index),
                item_type_hint or "all", len(sections),
            )
            # Persist the freshly-built index pair to disk so a new
            # adapter or an app restart can boot warm.
            self._try_save_persisted_path_index(
                scope_tag, tail_components, full_index, tail_index,
            )
            return full_index, tail_index
        finally:
            # ALWAYS signal the event so waiters can proceed even if
            # the build raised.
            event.set()

    def _try_load_persisted_path_index(
        self,
        scope_tag: str,
        tail_components: int,
    ) -> Optional[Tuple[Dict[str, str], Dict[str, str]]]:
        """Attempt to load the path indexes from playlist_cache.db.
        Returns ``(full_index, tail_index)`` on hit, ``None`` on
        miss or stale. Best-effort; any error returns None so the
        live build path always works."""
        try:
            from server import playlist_cache_db
            from services.tunables import (
                playlist_cache_path_index_max_age_seconds,
            )
            max_age = float(playlist_cache_path_index_max_age_seconds())
            if max_age <= 0:
                # Persistence disabled by tunable.
                return None
            server_id = self._machine_id_cached or ""
            if not server_id:
                return None
            loaded = playlist_cache_db.load_library_path_index(
                server_id, scope_tag, tail_components,
                max_age_seconds=max_age,
            )
            if loaded is None:
                return None
            log.info(
                "_try_load_persisted_path_index: hit for (server=%s "
                "scope=%s tail=%d) -> %d entries (built %.0fs ago)",
                server_id, scope_tag, tail_components,
                loaded["entry_count"], time.time() - loaded["built_at"],
            )
            return loaded["full_index"], loaded["tail_index"]
        except Exception as exc:
            log.debug(
                "_try_load_persisted_path_index: failed (%s scope=%s): %s",
                getattr(self, "_machine_id_cached", "?"),
                scope_tag, exc,
            )
            return None

    def _try_save_persisted_path_index(
        self,
        scope_tag: str,
        tail_components: int,
        full_index: Dict[str, str],
        tail_index: Dict[str, str],
    ) -> None:
        """Persist freshly-built indexes to playlist_cache.db.
        Best-effort; failures log + return silently."""
        try:
            from server import playlist_cache_db
            from services.tunables import (
                playlist_cache_path_index_max_age_seconds,
            )
            if playlist_cache_path_index_max_age_seconds() <= 0:
                # Tunable disabled persistence.
                return
            server_id = self._machine_id_cached or ""
            if not server_id:
                return
            if not full_index and not tail_index:
                # Don't persist empty indexes (no point).
                return
            playlist_cache_db.save_library_path_index(
                server_id, scope_tag, tail_components,
                full_index, tail_index,
            )
        except Exception as exc:
            log.debug(
                "_try_save_persisted_path_index: failed: %s", exc,
            )

    def resolve_by_full_path(
        self,
        file_path: str,
        *,
        library_id: Optional[str] = None,
        item_type_hint: str = "",
    ) -> Optional[str]:
        """Tier 2 resolver: exact-match ``file_path`` against the
        destination server's items.

        Reuses the shared path-index walk via ``_build_path_indexes``
        so this tier + path-tail share one section walk."""
        if not file_path:
            return None
        # Mirror-first lookup.
        self._ensure_mirror_warmed_for(
            item_type_hint=item_type_hint, library_id=library_id,
        )
        mirror_hit = self._mirror_lookup_full_path(
            file_path, item_type_hint=item_type_hint,
        )
        if mirror_hit is not None:
            log.debug(
                "resolve_by_full_path: mirror hit -> %s "
                "(skipping live walk)", mirror_hit,
            )
            return mirror_hit
        full_index, _ = self._build_path_indexes(
            item_type_hint, tail_components=3, library_id=library_id,
        )
        hit = full_index.get(file_path)
        if hit is not None:
            return hit
        # Case-insensitive fallback for Windows-style paths.
        lc = file_path.lower()
        for k, v in full_index.items():
            if k.lower() == lc:
                return v
        return None

    def _build_artist_track_index(
        self,
        section: Any,
        item_type: str,
        group_title: str,
    ) -> Dict[str, List[Any]]:
        """One-shot bulk lookup of every track (or episode) for a
        given artist/show on a section. Returns a dict keyed by
        lowercased title → list of matching Track objects.

        Per-artist batching: one Plex query loads every track under
        one artist; the cache then serves O(1) per-title lookups for
        the rest of the batch.

        Falls back to a generic search if Plex doesn't support the
        artist filter. Logs the size of the cache built."""
        index: Dict[str, List[Any]] = {}
        if item_type == "track":
            filter_key = "artist.title"
        elif item_type == "episode":
            filter_key = "show.title"
        else:
            filter_key = ""
        items: List[Any] = []
        if filter_key:
            try:
                items = list(section.search(
                    libtype=item_type,
                    filters={filter_key: group_title},
                ) or [])
            except Exception as exc:
                log.debug(
                    "_build_artist_track_index: filter search failed "
                    "on %r (group=%r): %s — falling back to per-title",
                    getattr(section, "title", "?"), group_title, exc,
                )
                items = []
        # Defence-in-depth: type-filter + group_title match (some
        # plexapi versions ignore the filters dict and return mixed
        # results).
        for it in items:
            if item_type and (getattr(it, "type", "") or "") != item_type:
                continue
            grand = getattr(it, "grandparentTitle", "") or ""
            if grand and grand != group_title:
                continue
            t = (getattr(it, "title", "") or "").lower()
            if t:
                index.setdefault(t, []).append(it)
        log.info(
            "_build_artist_track_index: section=%r group=%r -> %d "
            "tracks indexed (%d distinct titles)",
            getattr(section, "title", "?"), group_title,
            sum(len(v) for v in index.values()), len(index),
        )
        return index

    def clear_artist_track_index_for_tests(self) -> None:
        """Test-only helper to drop the per-artist cache."""
        self._artist_track_indexes.clear()

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
        """Resolve a destination ratingKey by item hierarchy when the
        GUID match misses (the resolver's hierarchy tier).

        Plex exposes rich title + parent metadata, so this delegates
        to the strict fuzzy-title resolver: episodes match on
        ``show_title``, tracks match on ``artist`` + ``album``.
        ``season_number`` / ``episode_number`` are accepted for
        signature parity with the cross-backend caller and the
        Jellyfin/Emby resolver, but Plex's title search does not key
        on them - strict mode means an ambiguous result (a same-titled
        episode across seasons) returns None rather than a wrong match.

        Returns a single ratingKey, or None on miss / ambiguity."""
        if item_type not in ("episode", "track") or not title:
            return None
        matches = self.resolve_by_fuzzy_title(
            title,
            item_type=item_type,
            artist=artist,
            show_title=show_title,
            album=album,
            ambiguous_behavior="strict",
            library_id=library_id,
            item_type_hint=item_type,
        )
        if matches and len(matches) == 1:
            return matches[0]
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
        """Tier 4 (last-resort) resolver.

        Title-based search filtered by ``item_type``, ``artist``
        (tracks), or ``show_title`` (episodes). When multiple
        candidates survive the initial filter, narrow by ``album``
        and ``source_file_path`` before applying the
        ``ambiguous_behavior`` policy.

        Returns a list of destination ratingKeys:
          * ``None`` on miss OR strict-refusal of ambiguity
          * ``[rk]`` for unique / narrowed / first-pick matches
          * ``[rk1, rk2, ...]`` when ambiguous_behavior == 'all'
        """
        if not title:
            return None
        # Mirror-first lookup.
        # The mirror knows enough metadata (artist / show / album /
        # season / episode) to answer fuzzy queries in one SQL hit.
        effective_hint = item_type_hint or item_type
        self._ensure_mirror_warmed_for(
            item_type_hint=effective_hint, library_id=library_id,
        )
        mirror_hit = self._mirror_lookup_fuzzy_title(
            title,
            item_type=item_type or "",
            artist=artist or None,
            show_title=show_title or None,
            album=album or None,
            ambiguous_behavior=ambiguous_behavior,
        )
        if mirror_hit is not None:
            log.debug(
                "resolve_by_fuzzy_title: mirror hit -> %s "
                "(skipping live walk)", mirror_hit,
            )
            return mirror_hit
        # Scope to the right kind of section. ``item_type_hint`` takes
        # precedence; otherwise use the supplied ``item_type`` (since
        # fuzzy already has it for libtype filtering).
        sections = self._sections_for_item_type(
            effective_hint, library_id=library_id,
        )
        if not sections:
            return None
        # Track (candidate, source_section) so we can record the
        # library-of-truth after a successful pick.
        candidates: List[Tuple[Any, Any]] = []
        # When artist/show_title is known, use the per-artist cache to
        # look up the title in O(1) instead of a fresh section.search
        # per call. The cache is populated on the first track for a
        # given (section, artist) and
        # reused for every subsequent track by that same artist in
        # the same batch.
        group_title = ""
        if item_type == "track":
            group_title = artist or ""
        elif item_type == "episode":
            group_title = show_title or ""

        for section in sections:
            section_key = str(getattr(section, "key", "") or "")
            cache_key = (section_key, item_type or "", group_title)

            if group_title:
                # Per-artist (or per-show) cached lookup path.
                with self._cache_lock:
                    idx = self._artist_track_indexes.get(cache_key)
                if idx is None:
                    # Build outside lock to keep concurrent OTHER
                    # cache reads unblocked.
                    idx = self._build_artist_track_index(
                        section, item_type, group_title,
                    )
                    with self._cache_lock:
                        # First write wins; if a race already
                        # populated it, use that to free this one
                        # for GC.
                        existing = self._artist_track_indexes.get(
                            cache_key,
                        )
                        if existing is None:
                            self._artist_track_indexes[cache_key] = idx
                        else:
                            idx = existing
                matches = idx.get(title.lower(), [])
                for r in matches:
                    candidates.append((r, section))
                continue
            # No artist/show pivot available — fall back to the
            # generic per-title search.
            try:
                if item_type:
                    results = section.search(title=title, libtype=item_type)
                else:
                    results = section.search(title=title)
            except Exception as exc:
                log.debug(
                    "resolve_by_fuzzy_title: section.search failed on %r: %s",
                    getattr(section, "title", "?"), exc,
                )
                continue
            for r in results or []:
                if item_type and (getattr(r, "type", "") or "") != item_type:
                    continue
                candidates.append((r, section))

        if not candidates:
            return None
        if len(candidates) == 1:
            cand, sec = candidates[0]
            rk = getattr(cand, "ratingKey", None)
            if rk is None:
                return None
            log.debug(
                "resolve_by_fuzzy_title: unique match for %r (type=%r) -> %s",
                title, item_type, rk,
            )
            self._record_library_of_truth(effective_hint, sec)
            return [str(rk)]

        # ── Multiple candidates: narrow before applying the policy ──
        # 1) Album tiebreaker (tracks). When the source's album is
        #    known, drop candidates whose parentTitle disagrees.
        if album and item_type == "track":
            narrowed = [
                (c, s) for (c, s) in candidates
                if (getattr(c, "parentTitle", "") or "") == album
            ]
            if narrowed:
                if len(narrowed) == 1:
                    cand, sec = narrowed[0]
                    rk = getattr(cand, "ratingKey", None)
                    if rk is not None:
                        log.debug(
                            "resolve_by_fuzzy_title: album tiebreaker "
                            "narrowed %d -> 1 for %r album=%r -> %s",
                            len(candidates), title, album, rk,
                        )
                        self._record_library_of_truth(effective_hint, sec)
                        return [str(rk)]
                candidates = narrowed

        # 2) Path-tail tiebreaker. Compare the source's normalised
        #    path-tail against each candidate's media path.
        if len(candidates) > 1 and source_file_path:
            src_parts = _normalize_path_parts(source_file_path)
            best: Any = None
            best_sec: Any = None
            best_score = 0
            tied = False
            for c, s in candidates:
                cand_fp = _safe_file_path(c) or ""
                if not cand_fp:
                    continue
                cand_parts = _normalize_path_parts(cand_fp)
                score = 0
                for i in range(1, min(len(src_parts), len(cand_parts)) + 1):
                    if src_parts[-i] == cand_parts[-i]:
                        score += 1
                    else:
                        break
                if score > best_score:
                    best = c
                    best_sec = s
                    best_score = score
                    tied = False
                elif score == best_score and best is not None:
                    tied = True
            if best is not None and not tied and best_score >= 2:
                rk = getattr(best, "ratingKey", None)
                if rk is not None:
                    log.debug(
                        "resolve_by_fuzzy_title: path-tail tiebreaker "
                        "picked %r (score=%d) from %d candidates -> %s",
                        title, best_score, len(candidates), rk,
                    )
                    self._record_library_of_truth(effective_hint, best_sec)
                    return [str(rk)]

        # ── Still ambiguous: apply the operator policy ──
        mode = (ambiguous_behavior or "strict").lower()
        if mode == "first":
            cand, sec = candidates[0]
            rk = getattr(cand, "ratingKey", None)
            if rk is not None:
                log.debug(
                    "resolve_by_fuzzy_title: AMBIGUOUS — %d candidates "
                    "for %r; 'first' policy picked %s",
                    len(candidates), title, rk,
                )
                self._record_library_of_truth(effective_hint, sec)
                return [str(rk)]
            return None
        if mode == "all":
            rks: List[str] = []
            first_sec: Any = None
            for c, s in candidates:
                rk = getattr(c, "ratingKey", None)
                if rk is None:
                    continue
                rks.append(str(rk))
                if first_sec is None:
                    first_sec = s
            if rks:
                log.debug(
                    "resolve_by_fuzzy_title: AMBIGUOUS — %d candidates "
                    "for %r; 'all' policy returning %d ids",
                    len(candidates), title, len(rks),
                )
                if first_sec is not None:
                    self._record_library_of_truth(effective_hint, first_sec)
                return rks
            return None
        # strict (default): refuse to guess.
        log.debug(
            "resolve_by_fuzzy_title: AMBIGUOUS — %d candidates for %r "
            "(type=%r, artist=%r, album=%r); strict policy refusing",
            len(candidates), title, item_type, artist, album,
        )
        return None
