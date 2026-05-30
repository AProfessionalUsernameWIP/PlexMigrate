"""Backend-agnostic affinity translation.

Per-user item affinity has two faces:

  * a numeric rating - Plex ``userRating``, a 0-10 float;
  * a binary favorite - Jellyfin / Emby ``IsFavorite``.

A snapshot stores both faithfully: the rating face, the favorite
face, and ``None`` for whichever face the source backend has no
source for (Plex captures leave ``is_favorite`` NULL; it has no
per-item favorite).

When that neutral affinity record is restored onto a destination
whose native model differs, the value must be translated. This
module is the single chokepoint for that translation. It is pure:
no I/O, no hidden state, no imports beyond the standard library, so
the restore / direct-transfer / sync write paths can all call it and
it can be unit-tested directly.

Direction summary:

  * destination is Plex (rating only): write a numeric rating. Use
    the source rating when present; otherwise, when the source item
    was favorited, write ``favorite_as_rating_value`` in its place.
  * destination is Jellyfin / Emby (favorite + numeric rating):
    write ``is_favorite`` - the source's own favorite flag when the
    source backend had one, otherwise derived from
    ``rating >= favorite_threshold``. Also pass the source's numeric
    rating through when it had one (J/E expose a numeric
    ``UserData.Rating``).

A derived favorite only ever ADDS a favorite. When the favorite is
derived from a rating (the source backend had no favorite flag of
its own), a sub-threshold rating yields "do not touch the favorite
flag" rather than an explicit un-favorite, so a restore never
clobbers a favorite the destination user set themselves. An explicit
favorite carried by a Jellyfin / Emby source IS restored exactly,
including an explicit ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Backends that expose a per-user binary favorite.
_FAVORITE_BACKENDS = frozenset({"jellyfin", "emby"})


@dataclass(frozen=True)
class AffinityWrite:
    """What to write on the destination for one item's affinity.

    ``rating`` is a numeric 0-10 value to write, or ``None`` to not
    write a rating at all. ``is_favorite`` is the favorite flag to
    write (``True`` / ``False``), or ``None`` to leave the
    destination's favorite flag untouched.
    """
    rating: Optional[float] = None
    is_favorite: Optional[bool] = None

    @property
    def wrote_anything(self) -> bool:
        """False when the source carried no affinity worth writing -
        the caller skips the item rather than counting an empty
        write."""
        return self.rating is not None or self.is_favorite is not None


def rating_to_favorite(
    rating: Optional[float], threshold: float,
) -> bool:
    """True when a numeric rating clears the favorite threshold.

    A ``None`` rating, or a non-numeric value, is never a favorite.
    """
    if rating is None:
        return False
    try:
        return float(rating) >= float(threshold)
    except (TypeError, ValueError):
        return False


def favorite_to_rating(
    is_favorite: Optional[bool], favorite_rating: float,
) -> Optional[float]:
    """The numeric rating that represents a favorite on a rating-only
    backend. ``None`` when the item is not favorited (nothing to
    write). The returned value is clamped to the 0-10 scale.
    """
    if not is_favorite:
        return None
    try:
        return max(0.0, min(10.0, float(favorite_rating)))
    except (TypeError, ValueError):
        return None


def affinity_row_is_meaningful(
    rating: Optional[float],
    is_favorite: Optional[bool],
) -> bool:
    """True when a per-user affinity row carries information worth
    persisting: a genuine numeric rating (> 0) OR a favorite flag.

    A row that is neither - no rating, or a rating of 0, and not
    favorited - represents the *absence* of any affinity and is noise
    in the snapshot / media DB. This is the single predicate the
    capture and ingest paths share so they agree on what counts as a
    meaningful row; ``has_rating`` uses the same ``not None and > 0``
    rule as :func:`translate_affinity` (see ``has_rating`` there) so
    the capture-side filter and the restore-side translation never
    disagree.

    ``is_favorite`` is ``None`` for backends with no favorite concept
    (Plex); pass it through unchanged - ``None`` contributes nothing,
    leaving the numeric rating as the only signal.
    """
    has_rating = rating is not None
    if has_rating:
        try:
            has_rating = float(rating) > 0
        except (TypeError, ValueError):
            has_rating = False
    return bool(has_rating or is_favorite)


def translate_affinity(
    *,
    source_rating: Optional[float],
    source_is_favorite: Optional[bool],
    dest_backend: str,
    favorite_threshold: float = 5.0,
    favorite_as_rating_value: float = 10.0,
) -> AffinityWrite:
    """Translate one item's neutral affinity into the write spec for
    ``dest_backend``.

    ``source_rating`` is the captured numeric rating (``None`` when
    the source had none). ``source_is_favorite`` is the captured
    favorite flag: ``True`` / ``False`` when the source backend has a
    favorite concept (Jellyfin / Emby), ``None`` when it does not
    (Plex).
    """
    dest = (dest_backend or "").strip().lower()
    has_rating = source_rating is not None and float(source_rating) > 0

    if dest in _FAVORITE_BACKENDS:
        if source_is_favorite is not None:
            # The source backend carried a real favorite flag
            # (Jellyfin / Emby source): restore it exactly, including
            # an explicit un-favorite.
            fav: Optional[bool] = bool(source_is_favorite)
        elif rating_to_favorite(source_rating, favorite_threshold):
            # The source had no favorite concept (Plex source): a
            # rating at or above the threshold ADDS a favorite.
            fav = True
        else:
            # Derived favorite below the threshold: leave the
            # destination's favorite flag untouched rather than
            # clobbering a favorite the destination user set.
            fav = None
        rating_out = float(source_rating) if has_rating else None
        return AffinityWrite(rating=rating_out, is_favorite=fav)

    # Destination is Plex, or any rating-only / unknown backend: the
    # numeric rating is the only field we can land.
    if has_rating:
        return AffinityWrite(
            rating=float(source_rating), is_favorite=None,
        )
    fav_rating = favorite_to_rating(
        source_is_favorite, favorite_as_rating_value,
    )
    return AffinityWrite(rating=fav_rating, is_favorite=None)
