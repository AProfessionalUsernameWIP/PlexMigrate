"""Plex smart-playlist mixin.

Smart playlists are server-side filter specs that Plex re-evaluates
on every read. The engine treats them as a separate kind: a
non-smart playlist serializes its items_tuple; a smart playlist
serializes its filter (and a vocabulary preflight against the
destination's tag id space).

Mixed into :class:`PlexAdapter`. Depends on ``self._server_for``
(per-user PlexServer factory) and on ``services.smart_playlist``
for the cross-server filter translation."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import UserContext


log = logging.getLogger("plexmigrate.services.adapters.plex")


class PlexSmartPlaylistMixin:
    """Smart-playlist read / write methods for :class:`PlexAdapter`.

    Owns the only places plexapi's smart-filter surface is touched:
    decode (``read_smart_playlist``), section-vocabulary preflight
    (``list_smart_filter_fields``, ``read_section_tag_choices``),
    and re-create (``create_smart_playlist``).

    The private ``_smart_dest_section`` resolver is kept here
    because nothing outside the smart-playlist surface needs it."""

    def read_smart_playlist(
        self,
        playlist_id: str,
        user_context: Optional[UserContext] = None,
    ) -> Optional[Any]:
        """Fetch a smart
        playlist and return a ``services.smart_playlist.RawSmartFilter``:
        the decoded filter (plexapi ``Playlist.filters()``) plus the
        source section's field-type + tag-choice maps, so
        ``services.smart_playlist.to_portable`` can translate it into
        a server-agnostic form.

        Returns None when the playlist is missing or not smart - a
        non-smart playlist has no filter to migrate. This is the only
        place plexapi's smart-filter surface is touched."""
        from services.smart_playlist import (
            RawSmartFilter, referenced_tag_fields,
        )
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
            log.debug("read_smart_playlist %s fetch failed: %s",
                      playlist_id, exc)
            return None
        if not bool(getattr(pl, "smart", False)):
            return None
        try:
            decoded = pl.filters()
        except Exception as exc:
            log.warning("read_smart_playlist %s: filters() failed: %s",
                        playlist_id, exc)
            return None
        try:
            section = pl.section()
        except Exception as exc:
            log.warning("read_smart_playlist %s: section() failed: %s",
                        playlist_id, exc)
            return None
        libtype = str(
            decoded.get("libtype") or getattr(section, "type", "") or ""
        )
        # Field-type map: {field: filterType} for this libtype.
        field_types: Dict[str, str] = {}
        try:
            for ff in section.listFilters(libtype) or []:
                fkey = getattr(ff, "filter", None)
                if fkey:
                    field_types[str(fkey)] = str(
                        getattr(ff, "filterType", "") or ""
                    )
        except Exception as exc:
            log.debug("read_smart_playlist %s: listFilters failed: %s",
                      playlist_id, exc)
        # Tag-choice maps {id: name}, only for tag fields the filter
        # actually references (each is a live API round-trip).
        tag_choices: Dict[str, Dict[str, str]] = {}
        for tag_field in referenced_tag_fields(decoded, field_types):
            try:
                choices = section.listFilterChoices(tag_field, libtype) or []
                tag_choices[tag_field] = {
                    str(getattr(c, "key", "")): str(getattr(c, "title", ""))
                    for c in choices
                    if getattr(c, "key", None) is not None
                }
            except Exception as exc:
                log.debug(
                    "read_smart_playlist %s: listFilterChoices(%s) failed: %s",
                    playlist_id, tag_field, exc,
                )
                tag_choices[tag_field] = {}
        return RawSmartFilter(
            section_name=getattr(section, "title", "") or "",
            section_type=getattr(section, "type", "") or "",
            decoded=decoded,
            field_types=field_types,
            tag_choices=tag_choices,
            playlist_name=getattr(pl, "title", "") or "",
        )

    def _smart_dest_section(
        self, server: Any, section_name: str, section_type: str,
    ) -> Any:
        """Resolve the destination LibrarySection for a smart-playlist
        re-create / preflight: by name first, then by type. Raises
        ValueError when neither matches."""
        try:
            return server.library.section(section_name)
        except Exception:
            pass
        for s in server.library.sections():
            if (getattr(s, "type", "") or "") == section_type:
                return s
        raise ValueError(
            f"no library section named {section_name!r} (type "
            f"{section_type!r}) on the destination server"
        )

    def list_smart_filter_fields(
        self,
        *,
        section_name: str,
        section_type: str,
        libtype: Optional[str] = None,
        user_context: Optional[UserContext] = None,
    ) -> List[Dict[str, Any]]:
        """Enumerate the smart-playlist filter vocabulary for one
        library section + libtype: every filterable field, its value
        type, and the operators Plex accepts for it. Backs the dev
        console smart-playlist builder, so the UI only ever offers
        filters this server actually supports. Returns
        ``[{field, title, type, operators: [{key, title}]}]``."""
        server = self._server_for(user_context)
        section = self._smart_dest_section(server, section_name, section_type)
        lt = str(libtype or section_type or "")
        try:
            filters = section.listFilters(lt) or []
        except Exception as exc:
            log.warning(
                "list_smart_filter_fields: listFilters(%r) failed: %s",
                lt, exc,
            )
            return []
        ops_by_type: Dict[str, List[Dict[str, str]]] = {}

        def _ops(ftype: str) -> List[Dict[str, str]]:
            if ftype not in ops_by_type:
                rows: List[Dict[str, str]] = []
                try:
                    for op in (section.listOperators(ftype) or []):
                        key = getattr(op, "key", None)
                        if key is None:
                            continue
                        rows.append({
                            "key": str(key),
                            "title": str(getattr(op, "title", "") or key),
                        })
                except Exception as exc:
                    log.debug(
                        "list_smart_filter_fields: listOperators(%r) "
                        "failed: %s", ftype, exc,
                    )
                ops_by_type[ftype] = rows
            return ops_by_type[ftype]

        out: List[Dict[str, Any]] = []
        for ff in filters:
            field = str(getattr(ff, "filter", "") or "")
            if not field:
                continue
            ftype = str(getattr(ff, "filterType", "") or "")
            out.append({
                "field": field,
                "title": str(getattr(ff, "title", "") or field),
                "type": ftype,
                "operators": _ops(ftype),
            })
        return out

    def create_smart_playlist(
        self,
        *,
        title: str,
        section_name: str,
        section_type: str,
        libtype: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        sort: Optional[List[str]] = None,
        limit: Optional[int] = None,
        user_context: Optional[UserContext] = None,
    ) -> str:
        """Create a smart
        playlist on this Plex server from a translated filter spec
        (the output of ``services.smart_playlist.from_portable``).
        plexapi's ``_validateFieldValueTag`` resolves each tag NAME in
        ``filters`` to this server's own tag id. Returns the new
        playlist's rating key."""
        from plexapi.playlist import Playlist
        server = self._server_for(user_context)
        section = self._smart_dest_section(
            server, section_name, section_type,
        )
        pl = Playlist.create(
            server, title, section=section, smart=True,
            libtype=(libtype or None), sort=(sort or None),
            limit=limit, filters=(filters or None),
        )
        return str(getattr(pl, "ratingKey", "") or "")

    def read_section_tag_choices(
        self,
        *,
        section_name: str,
        section_type: str,
        tag_fields: List[str],
        libtype: Optional[str] = None,
        user_context: Optional[UserContext] = None,
    ) -> Dict[str, Dict[str, str]]:
        """Return
        ``{field: {id: name}}`` tag choices on this server's matching
        library section, for the preflight's unresolved-tag check
        against a migration destination."""
        server = self._server_for(user_context)
        try:
            section = self._smart_dest_section(
                server, section_name, section_type,
            )
        except ValueError:
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for fld in tag_fields or []:
            try:
                choices = section.listFilterChoices(fld, libtype) or []
                out[str(fld)] = {
                    str(getattr(c, "key", "")): str(getattr(c, "title", ""))
                    for c in choices
                    if getattr(c, "key", None) is not None
                }
            except Exception as exc:
                log.debug(
                    "read_section_tag_choices(%s) failed: %s", fld, exc,
                )
                out[str(fld)] = {}
        return out
