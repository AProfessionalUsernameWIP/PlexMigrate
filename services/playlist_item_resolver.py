"""4-tier source-to-destination playlist item resolution.

Extracted from ``services/playlist_copy.py``'s nested closure
``_resolve_one`` so the resolution kernel can serve more than the one
caller it grew up inside. The tier walk
(same-server passthrough -> GUID -> full-path -> path-tail ->
fuzzy-title) and its classification (which tier hit, did any tier
raise, what error strings) is now a neutral function; the
orchestration (cancel checks, thread-safe progress counters, slot
mutation, tier-hit progress emit) stays in ``copy_playlist``.

Results-equivalent extraction: the resolver returns the same
destination ``ItemRef``\\ s, the same tier hit, the same error
strings, and the same skip-vs-fail classification as the inline
closure did. Pinned by the 43 characterization tests in
``dev_docs/tests_backend/test_playlist_copy.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from services.adapters import ItemRef


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of one :func:`resolve_item_to_dest` call.

    ``dest_refs`` is empty on a miss; ``len == 1`` for a unique tier
    hit or the same-server passthrough; ``len > 1`` when fuzzy
    'all'-mode expanded one source item into multiple destination ids.

    ``tier_hit`` is the resolver tier that landed the match
    (``"GUID"`` / ``"full-path"`` / ``"path-tail"`` / ``"fuzzy-title"``);
    ``None`` for same-server passthrough (no live tier ran) and for
    clean misses. Callers use this to emit a per-tier progress event;
    keeping it ``None`` for same-server preserves the original
    behaviour of NOT emitting tier-hit on the passthrough fast-path.

    ``tier_raised`` is True when ANY tier in the walk raised - the
    signal that distinguishes a real failure from a clean
    no-destination-match miss in the caller's counter classification.

    ``methods_tried`` is the ordered list of resolver method names
    actually attempted on the destination; used to build the no-match
    error string.

    ``errors`` carries per-tier exception strings AND, on a no-match
    walk, the trailing 'no destination match - tried ...' string the
    end user sees in the run summary.
    """
    dest_refs: Tuple[ItemRef, ...] = ()
    tier_hit: Optional[str] = None
    tier_raised: bool = False
    methods_tried: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()


def resolve_item_to_dest(
    adapter: Any,
    ref: ItemRef,
    *,
    same_server: bool = False,
    fuzzy_ambiguous_behavior: str = "strict",
    logger: Optional[logging.Logger] = None,
) -> ResolveResult:
    """Resolve one source ``ref`` to destination :class:`ItemRef`\\ (s)
    on ``adapter`` via the multi-tier chain.

    Tiers, in order, short-circuiting on the first hit:

      0. **Same-server passthrough** - when ``same_server=True`` the
         source's ``backend_item_id`` IS the destination's id. Build the
         passthrough ref directly, no live API call. An item with no
         ``backend_item_id`` in same-server mode is a clean skip (no
         tier attempted, no error added).
      1. **GUID** - ``adapter.resolve_by_guids(guids, ...)``.
      2. **full-path** - ``adapter.resolve_by_full_path(file_path, ...)``;
         skipped when the adapter does not implement the method.
      3. **path-tail** - ``adapter.resolve_by_path_tail(file_path, ...)``;
         optional like full-path.
      4. **fuzzy-title** - ``adapter.resolve_by_fuzzy_title(title, ...)``;
         optional. Returns a LIST of ids; in 'all' ambiguity mode one
         source item expands into multiple destination refs.

    A tier that raises is recorded in ``errors`` with a descriptive
    string, marks ``tier_raised=True``, and the walk continues to the
    next tier - the same behavior the original closure had. The walk
    stops on the first hit; a later tier that does NOT run on a hit
    contributes no error even if it might have raised.
    """
    log = logger or _LOGGER

    # Tier 0: same-server passthrough.
    if same_server:
        if ref.backend_item_id:
            return ResolveResult(
                dest_refs=(ItemRef(
                    backend_item_id=ref.backend_item_id,
                    guids=tuple(ref.guids or ()),
                    title=ref.title,
                    file_path=ref.file_path,
                ),),
            )
        return ResolveResult()

    methods_tried: List[str] = []
    errors: List[str] = []
    tier_raised = False
    dest_id: Optional[str] = None
    tier_hit: Optional[str] = None
    fuzzy_dest_ids: Optional[List[str]] = None

    guids = tuple(ref.guids or ())
    _item_type_hint = getattr(ref, "item_type", "") or ""

    # Tier 1: GUID resolution.
    if guids:
        methods_tried.append("GUID")
        try:
            dest_id = adapter.resolve_by_guids(
                guids, item_type_hint=_item_type_hint,
            )
            if dest_id:
                tier_hit = "GUID"
        except Exception as exc:
            errors.append(f"resolve {ref.title!r} via GUID: {exc}")
            dest_id = None
            tier_raised = True

    # Tier 2: full-path exact match.
    if not dest_id and ref.file_path:
        _full_path = getattr(adapter, "resolve_by_full_path", None)
        if callable(_full_path):
            methods_tried.append("full-path")
            try:
                dest_id = _full_path(
                    ref.file_path, item_type_hint=_item_type_hint,
                )
                if dest_id:
                    tier_hit = "full-path"
            except Exception as exc:
                errors.append(
                    f"resolve {ref.title!r} via full-path: {exc}",
                )
                dest_id = None
                tier_raised = True

    # Tier 3: path-tail fallback.
    if not dest_id and ref.file_path:
        _tail = getattr(adapter, "resolve_by_path_tail", None)
        if callable(_tail):
            methods_tried.append("path-tail")
            try:
                dest_id = _tail(
                    ref.file_path, item_type_hint=_item_type_hint,
                )
                if dest_id:
                    tier_hit = "path-tail"
            except Exception as exc:
                errors.append(
                    f"resolve {ref.title!r} via path-tail: {exc}",
                )
                dest_id = None
                tier_raised = True

    # Tier 4: fuzzy-title (last resort).
    if not dest_id and ref.title:
        _fuzzy = getattr(adapter, "resolve_by_fuzzy_title", None)
        if callable(_fuzzy):
            methods_tried.append("fuzzy-title")
            try:
                fuzzy_dest_ids = _fuzzy(
                    ref.title,
                    item_type=getattr(ref, "item_type", "") or "",
                    artist=getattr(ref, "artist", "") or "",
                    show_title=getattr(ref, "show_title", "") or "",
                    album=getattr(ref, "album", "") or "",
                    source_file_path=ref.file_path or "",
                    ambiguous_behavior=fuzzy_ambiguous_behavior,
                    item_type_hint=_item_type_hint,
                )
                if fuzzy_dest_ids:
                    tier_hit = "fuzzy-title"
                    dest_id = fuzzy_dest_ids[0]
            except Exception as exc:
                errors.append(
                    f"resolve {ref.title!r} via fuzzy-title: {exc}",
                )
                fuzzy_dest_ids = None
                dest_id = None
                tier_raised = True

    # No tier landed a destination id -> miss. Append the descriptive
    # no-match string and return with whatever tier_raised carried.
    if not dest_id:
        methods_label = (
            ", ".join(methods_tried) if methods_tried
            else "none (item has no GUIDs / file_path / title)"
        )
        errors.append(
            f"{ref.title!r}: no destination match — "
            f"tried {methods_label}"
        )
        return ResolveResult(
            dest_refs=(),
            tier_hit=None,
            tier_raised=tier_raised,
            methods_tried=tuple(methods_tried),
            errors=tuple(errors),
        )

    # Match: expand into ItemRef(s). Fuzzy 'all'-mode produces N ids
    # for one source item; every other tier produces exactly one.
    expanded_ids: List[str] = fuzzy_dest_ids if fuzzy_dest_ids else [dest_id]
    dest_refs = tuple(
        ItemRef(
            backend_item_id=_did,
            guids=guids,
            title=ref.title,
            file_path=ref.file_path,
            item_type=getattr(ref, "item_type", "") or "",
            artist=getattr(ref, "artist", "") or "",
            show_title=getattr(ref, "show_title", "") or "",
            album=getattr(ref, "album", "") or "",
        )
        for _did in expanded_ids
    )
    return ResolveResult(
        dest_refs=dest_refs,
        tier_hit=tier_hit,
        tier_raised=tier_raised,
        methods_tried=tuple(methods_tried),
        errors=tuple(errors),
    )
