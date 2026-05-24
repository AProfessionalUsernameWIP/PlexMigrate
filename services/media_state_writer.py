"""Cross-backend media-state write pipeline.

One place that owns "how do I land a play-count or a rating on a
destination server", so the restore engines and the upcoming
direct-alter features (set an item's play count / rating straight from
the UI) share a single implementation instead of duplicating the merge
math and the translate-then-write dance.

Two concerns live here:

  * Play count - a stored count is reconciled against the
    destination's current count into the ABSOLUTE target value that
    :meth:`MediaServerAdapter.set_watched` expects (the adapter takes
    an absolute target, never a delta). The reconciliation honours a
    restore-style mode / strategy ("replace", "higher", "sum").

  * Affinity (rating + favorite) - a neutral per-user affinity is run
    through :func:`services.backend_translation.translate_affinity` to
    get what the destination backend can actually store (a numeric
    rating for Plex; a favorite flag, plus optional rating, for
    Jellyfin / Emby) and then written through the adapter.

Nothing here is restore-specific: callers pass an already-resolved
``ItemRef`` and ``UserContext`` and get back neutral ``WriteResult``
objects. Counting, logging context, and destination-item resolution
stay with the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from services.adapters import ItemRef, MediaServerAdapter, UserContext, WriteResult
from services.backend_translation import translate_affinity

_LOGGER = logging.getLogger(__name__)


# ── Play count ───────────────────────────────────────────────────────────────

def resolve_view_count_target(
    *,
    stored_view_count: int,
    current_view_count: Optional[int],
    mode: str = "merge",
    strategy: str = "higher",
) -> int:
    """Reconcile a stored play count into the absolute target to write.

    :meth:`MediaServerAdapter.set_watched` takes an absolute
    ``view_count`` target, never a delta. This turns the stored count
    plus the destination's current count into that target:

      * Replace mode, or an unreadable current count: the stored count
        IS the target. (An unreadable current - the Jellyfin / Emby
        base default returns None - cannot feed strategy math, so it
        falls back to the stored count, the pre-RESTORE-04 behaviour.)
      * Merge + "sum": ``current + stored``.
      * Merge + "higher" (default): ``max(stored, current)``.

    Negative inputs are clamped to 0.
    """
    is_replace = str(mode or "merge").lower() == "replace"
    strat = str(strategy or "higher").lower()
    stored = max(0, int(stored_view_count))
    if is_replace or current_view_count is None:
        return stored
    current = max(0, int(current_view_count))
    if strat == "sum":
        return current + stored
    return max(stored, current)


def read_current_view_count(
    adapter: MediaServerAdapter,
    item_ref: ItemRef,
    *,
    user_context: UserContext,
    logger: Optional[logging.Logger] = None,
) -> Optional[int]:
    """Best-effort read of the destination's current play count.

    Wraps ``adapter.get_current_view_count`` so a backend that cannot
    read the count cheaply (the base default returns None) and one
    that raises mid-read both collapse to None - the signal
    :func:`resolve_view_count_target` reads as "fall back to the
    stored count". Never raises.
    """
    log = logger or _LOGGER
    try:
        return adapter.get_current_view_count(
            item_ref, user_context=user_context,
        )
    except Exception as exc:
        log.debug(
            "media_state_writer: get_current_view_count failed for %r: "
            "%s; treating current count as unreadable.",
            item_ref.title or item_ref.backend_item_id, exc,
        )
        return None


@dataclass(frozen=True)
class PlayCountOutcome:
    """Outcome of one :func:`apply_play_count` call.

    ``target_view_count`` is the absolute count that was written;
    ``current_view_count`` is what the destination held beforehand
    (None when it could not be read)."""
    result: WriteResult
    target_view_count: int
    current_view_count: Optional[int]


def apply_play_count(
    adapter: MediaServerAdapter,
    item_ref: ItemRef,
    *,
    stored_view_count: int,
    user_context: UserContext,
    last_viewed_at: Optional[float] = None,
    mode: str = "merge",
    strategy: str = "higher",
    logger: Optional[logging.Logger] = None,
) -> PlayCountOutcome:
    """Land a play count on ``item_ref`` via ``adapter``.

    Reads the destination's current count once, reconciles it against
    ``stored_view_count`` with :func:`resolve_view_count_target`, and
    writes the absolute target through ``adapter.set_watched``. The
    current count is also forwarded to ``set_watched`` so the Plex
    adapter can do its exact unscrobble + scrobble math.
    """
    current = read_current_view_count(
        adapter, item_ref, user_context=user_context, logger=logger,
    )
    target = resolve_view_count_target(
        stored_view_count=stored_view_count,
        current_view_count=current,
        mode=mode,
        strategy=strategy,
    )
    result = adapter.set_watched(
        item_ref,
        view_count=target,
        last_viewed_at=last_viewed_at,
        user_context=user_context,
        current_view_count=current,
    )
    return PlayCountOutcome(
        result=result,
        target_view_count=target,
        current_view_count=current,
    )


# ── Affinity (rating + favorite) ─────────────────────────────────────────────

@dataclass(frozen=True)
class AffinityOutcome:
    """Outcome of one :func:`apply_affinity` call.

    A neutral affinity can drive up to two backend writes - a numeric
    rating and a favorite toggle. Each ``*_result`` is None when that
    face was not written: either the source affinity had nothing for
    it, or ``translate_affinity`` decided the destination backend
    cannot store it. ``wrote_anything`` is False when the translated
    affinity was empty and no adapter call was made."""
    rating_result: Optional[WriteResult]
    favorite_result: Optional[WriteResult]
    wrote_anything: bool


def apply_affinity(
    adapter: MediaServerAdapter,
    item_ref: ItemRef,
    *,
    source_rating: Optional[float],
    source_is_favorite: Optional[bool],
    user_context: UserContext,
    favorite_threshold: float = 5.0,
    favorite_as_rating_value: float = 10.0,
) -> AffinityOutcome:
    """Translate a neutral affinity and write it to ``item_ref``.

    ``source_rating`` (0.0-10.0) and ``source_is_favorite`` are the
    backend-neutral affinity. They are run through ``translate_affinity``
    for ``adapter.backend``, which decides which face(s) the
    destination can store; the result is written - ``set_rating`` for a
    numeric rating, ``set_favorite`` for the favorite toggle. Either or
    both may fire.

    When the translated affinity is empty (nothing meaningful to write)
    no adapter call is made and ``wrote_anything`` is False.
    """
    spec = translate_affinity(
        source_rating=source_rating,
        source_is_favorite=source_is_favorite,
        dest_backend=adapter.backend,
        favorite_threshold=favorite_threshold,
        favorite_as_rating_value=favorite_as_rating_value,
    )
    if not spec.wrote_anything:
        return AffinityOutcome(
            rating_result=None, favorite_result=None, wrote_anything=False,
        )
    rating_result: Optional[WriteResult] = None
    favorite_result: Optional[WriteResult] = None
    if spec.rating is not None:
        rating_result = adapter.set_rating(
            item_ref, float(spec.rating), user_context=user_context,
        )
    if spec.is_favorite is not None:
        favorite_result = adapter.set_favorite(
            item_ref, bool(spec.is_favorite), user_context=user_context,
        )
    return AffinityOutcome(
        rating_result=rating_result,
        favorite_result=favorite_result,
        wrote_anything=True,
    )
