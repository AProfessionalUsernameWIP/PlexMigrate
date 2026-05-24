"""
Mixed-media playlist classification + strategy driver.

Background
----------
Jellyfin and Emby allow a single playlist to mix media types
(audio + video + photo). Plex forbids it: a "playlist" is strictly
one of audio / video / photo. When restoring a J/E-source mixed
playlist to a Plex destination, the engine has to decide what to do
before any item write.

This module is the decision layer:

* :func:`classify` - pure function that bucketises a list of source
  items by Plex playlist-type family and reports dominance.
* :func:`apply_strategy` - takes a classification + the end user's
  config and produces a list of (playlist_name, items_subset,
  target_libraries_hint) tuples describing what to write.

The driver does NOT itself create playlists; it just produces the
plan that the existing restorer's playlist creation loop consumes.

End user-locked decisions (Plan section "End user answers"):
- behavior: skip / dominant / split (default skip)
- dominance_threshold: 0.0-1.0 (default 0.60)
- video_routing: library_agnostic / library_dominant (default library_agnostic)
- logging: full / decisions_only / off
- collision_handling: duplicate / suffix / skip (default duplicate)

Tied families with no dominant winner ALWAYS fall back to split
(per locked answer Q5). `library_dominant` on a movies+TV tie falls
back to `library_agnostic` (locked answer Q2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


log = logging.getLogger("plexmigrate.services.mixed_media")


# ── Item-type → Plex playlist family map ────────────────────────────────────
#
# Mirrors services.restorer._ITEM_TYPE_TO_PLEX_PLAYLIST_TYPE but
# kept independent here to avoid importing the Plex-only restorer
# from a backend-agnostic module. Updates flow into both maps when
# Plex adds a new leaf type.

_ITEM_TYPE_TO_FAMILY: Dict[str, str] = {
    "track":      "audio",
    "album":      "audio",
    "artist":     "audio",
    "audio":      "audio",
    "audiobook":  "audio",
    "book":       "audio",        # book playlists are audiobooks on Plex
    "movie":      "video",
    "episode":    "video",
    "show":       "video",
    "season":     "video",
    "clip":       "video",
    "musicvideo": "video",
    "photo":      "picture",
}


# Subdivisions of the "video" family for library_dominant routing.
# Movies + TV are the two real video sub-types; everything else
# (clips, music videos) routes generically.
_VIDEO_SUBTYPE: Dict[str, str] = {
    "movie": "movies",
    "episode": "tv",
    "show": "tv",
    "season": "tv",
}


# ── Classification ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MixedMediaClassification:
    """Result of bucketising a playlist's items by Plex playlist family.

    Pure data; produced by :func:`classify`. Consumed by
    :func:`apply_strategy` and surfaced in structured logs."""
    is_mixed: bool                      # items span >= 2 families
    family_counts: Dict[str, int]       # {audio: N, video: M, picture: K}
    items_by_family: Dict[str, List[Any]]  # per-family item lists, original order
    dominant_family: Optional[str]      # winning family or None on tie
    tied_families: List[str]            # families at the top when no single dominant
    crossed_threshold: bool             # dominant_family meets threshold
    video_movies_count: int             # for library_dominant routing decisions
    video_tv_count: int


def classify(
    items: Iterable[Any],
    *,
    threshold: float = 0.60,
) -> MixedMediaClassification:
    """Bucketise ``items`` by Plex playlist family. Each item is a dict
    or object with a ``type`` attribute / key (the snapshot serialiser
    emits dicts; restorer-side resolved items may be objects).

    ``threshold`` is the dominance threshold from the tunable; an
    item out of [0,1] is clamped to that range. Returns a
    :class:`MixedMediaClassification` carrying the family histogram +
    decisions the strategy driver needs.

    Items with an unrecognised type are dropped from the count (they
    can't participate in a Plex playlist anyway). An empty / all-
    unknown list returns ``is_mixed=False`` with no dominant family;
    the caller treats this as "no decision needed."""
    t = max(0.0, min(1.0, float(threshold)))
    family_counts: Dict[str, int] = {}
    items_by_family: Dict[str, List[Any]] = {}
    video_movies = 0
    video_tv = 0
    for it in items:
        leaf = _item_type(it)
        family = _ITEM_TYPE_TO_FAMILY.get(leaf)
        if family is None:
            continue
        family_counts[family] = family_counts.get(family, 0) + 1
        items_by_family.setdefault(family, []).append(it)
        if family == "video":
            sub = _VIDEO_SUBTYPE.get(leaf)
            if sub == "movies":
                video_movies += 1
            elif sub == "tv":
                video_tv += 1

    total = sum(family_counts.values())
    is_mixed = len(family_counts) >= 2
    dominant: Optional[str] = None
    tied: List[str] = []
    crossed = False
    if family_counts:
        sorted_families = sorted(
            family_counts.items(), key=lambda kv: (-kv[1], kv[0]),
        )
        top_count = sorted_families[0][1]
        top_families = [fam for fam, n in sorted_families if n == top_count]
        if len(top_families) == 1:
            dominant = top_families[0]
            crossed = (top_count / total) >= t if total > 0 else False
        else:
            tied = top_families

    return MixedMediaClassification(
        is_mixed=is_mixed,
        family_counts=family_counts,
        items_by_family=items_by_family,
        dominant_family=dominant,
        tied_families=tied,
        crossed_threshold=crossed,
        video_movies_count=video_movies,
        video_tv_count=video_tv,
    )


def _item_type(it: Any) -> str:
    """Read the leaf type off an item, normalised to lowercase. Accepts
    both dict-style snapshot rows and adapter-style objects."""
    if isinstance(it, dict):
        return str(it.get("type") or "").strip().lower()
    return str(getattr(it, "type", "") or "").strip().lower()


# ── Strategy driver ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StrategyOutput:
    """One write directive produced by the strategy driver.

    ``items`` is the subset to put in this playlist. ``name`` is the
    final playlist name (with any end user suffix applied).
    ``target_libraries_hint`` is a list of library-type filters
    ("movies" / "tv" / None=any) the restorer can use when picking
    which library to write into; None means library_agnostic for that
    output."""
    name: str
    items: List[Any]
    family: str                       # "audio" / "video" / "picture"
    target_libraries_hint: Optional[List[str]] = None
    # Reason captured for the structured log line ("uniform" /
    # "dominant" / "split:audio" / etc.).
    reason: str = ""


# Resolved per-run config the strategy driver acts on.
@dataclass(frozen=True)
class MixedMediaConfig:
    behavior: str = "skip"                  # skip | dominant | split
    dominance_threshold: float = 0.60       # 0.0 - 1.0
    video_routing: str = "library_agnostic" # library_agnostic | library_dominant
    logging: str = "full"                   # full | decisions_only | off
    collision_handling: str = "duplicate"   # duplicate | suffix | skip


def apply_strategy(
    *,
    playlist_name: str,
    classification: MixedMediaClassification,
    config: MixedMediaConfig,
    logger: Optional[logging.Logger] = None,
) -> List[StrategyOutput]:
    """Convert a classification + end user config into a list of write
    directives.

    Empty list means "skip this playlist entirely" (the end user chose
    skip mode, or all items fell to unknown types, or the resolved
    playlist would be empty for some other reason). The caller logs
    the empty outcome at the appropriate level.

    Behaviour matrix (per Plan section B.2):

    | classification           | behavior | output                              |
    |--------------------------|----------|-------------------------------------|
    | not mixed                | any      | original (uniform single-type)      |
    | mixed + crossed          | skip     | empty                               |
    | mixed + crossed          | dominant | one playlist, dominant items only   |
    | mixed + crossed          | split    | N playlists, suffixed names         |
    | mixed + tied (no winner) | skip     | empty                               |
    | mixed + tied (no winner) | dominant | falls back to split for tied types  |
    | mixed + tied (no winner) | split    | N playlists for tied types only     |
    """
    logger = logger or log
    behavior = (config.behavior or "skip").lower()
    log_mode = (config.logging or "full").lower()

    # Non-mixed playlists pass through untouched. The "uniform" reason
    # is what the structured log records when logging=full; nothing
    # surfaces under decisions_only.
    if not classification.is_mixed:
        family = (
            classification.dominant_family
            or (next(iter(classification.family_counts), "")
                if classification.family_counts else "")
        )
        items = []
        for fam_items in classification.items_by_family.values():
            items.extend(fam_items)
        if log_mode == "full":
            logger.info(
                "[mixed-media] %r: uniform single-type (family=%r); pass-through.",
                playlist_name, family,
            )
        return [StrategyOutput(
            name=playlist_name, items=items, family=family,
            target_libraries_hint=_resolve_video_hint(
                family, classification, config,
            ),
            reason="uniform",
        )]

    # From here on, the playlist IS mixed. Decision tree.
    family_summary = dict(classification.family_counts)
    has_winner = (
        classification.dominant_family is not None
        and classification.crossed_threshold
    )

    if behavior == "skip":
        if log_mode in ("full", "decisions_only"):
            logger.info(
                "[mixed-media] %r: mixed (family_counts=%s, behavior=skip); "
                "skipping playlist.",
                playlist_name, family_summary,
            )
        return []

    if behavior == "dominant" and has_winner:
        items = list(classification.items_by_family.get(
            classification.dominant_family, []  # type: ignore[arg-type]
        ))
        if log_mode in ("full", "decisions_only"):
            logger.info(
                "[mixed-media] %r: mixed (family_counts=%s); behavior=dominant; "
                "writing dominant=%r (%d items).",
                playlist_name, family_summary,
                classification.dominant_family, len(items),
            )
        if not items:
            return []
        return [StrategyOutput(
            name=playlist_name,
            items=items,
            family=classification.dominant_family,  # type: ignore[arg-type]
            target_libraries_hint=_resolve_video_hint(
                classification.dominant_family, classification, config,  # type: ignore[arg-type]
            ),
            reason="dominant",
        )]

    # Either behavior == split, OR behavior == dominant on a tie
    # (locked Q1: fall back to split for tied types only).
    families_to_emit: List[str]
    if behavior == "dominant" and not has_winner:
        # Tied: emit only tied families per locked decision.
        families_to_emit = list(classification.tied_families)
        reason_base = "dominant_tied_split"
    else:
        # Pure split mode: emit every present family.
        families_to_emit = sorted(classification.family_counts.keys())
        reason_base = "split"

    out: List[StrategyOutput] = []
    for fam in families_to_emit:
        fam_items = list(classification.items_by_family.get(fam, []))
        if not fam_items:
            continue
        suffix = _family_suffix(fam)
        name = f"{playlist_name} {suffix}".strip()
        out.append(StrategyOutput(
            name=name,
            items=fam_items,
            family=fam,
            target_libraries_hint=_resolve_video_hint(
                fam, classification, config,
            ),
            reason=f"{reason_base}:{fam}",
        ))

    if log_mode in ("full", "decisions_only"):
        logger.info(
            "[mixed-media] %r: mixed (family_counts=%s); behavior=%s; "
            "writing %d split playlist(s): %s",
            playlist_name, family_summary, behavior,
            len(out),
            [(o.name, o.family, len(o.items)) for o in out],
        )
    return out


def _family_suffix(family: str) -> str:
    """Per-family display suffix used by split mode. Matches the Plan's
    "Workout [Audio]" style end user-facing naming."""
    return {
        "audio": "[Audio]",
        "video": "[Video]",
        "picture": "[Photo]",
    }.get(family, f"[{family.title()}]")


def _resolve_video_hint(
    family: str,
    classification: MixedMediaClassification,
    config: MixedMediaConfig,
) -> Optional[List[str]]:
    """Compute the library-routing hint for a video-family output.

    None for non-video families OR library_agnostic mode OR a tied
    movies+TV count (locked Q2: tie -> library_agnostic). For
    library_dominant + a clear winner, returns the single-element
    list ["movies"] or ["tv"]."""
    if family != "video":
        return None
    mode = (config.video_routing or "library_agnostic").lower()
    if mode != "library_dominant":
        return None
    movies = classification.video_movies_count
    tv = classification.video_tv_count
    if movies == tv:
        return None
    return ["movies" if movies > tv else "tv"]


# ── Collision-handling helper ──────────────────────────────────────────────


def resolve_collision_name(
    proposed_name: str,
    *,
    existing_names: Iterable[str],
    collision_handling: str = "duplicate",
) -> Optional[str]:
    """Given a proposed playlist name + the destination's current
    playlist roster, return either the final name to use or ``None``
    if the end user opted to skip on collision.

    - ``duplicate`` (default, matches existing engine): always return
      proposed_name; multiple playlists with the same name are
      allowed.
    - ``suffix``: if proposed_name exists, append ``(2)``, ``(3)``,
      etc. until a free name is found.
    - ``skip``: if proposed_name exists, return None.

    ``existing_names`` is consumed greedily; lowercase-normalised for
    case-insensitive comparison."""
    existing = {(n or "").strip().lower() for n in existing_names}
    mode = (collision_handling or "duplicate").lower()
    needle = (proposed_name or "").strip().lower()
    if needle not in existing:
        return proposed_name
    if mode == "duplicate":
        return proposed_name
    if mode == "skip":
        return None
    if mode == "suffix":
        i = 2
        while True:
            candidate = f"{proposed_name} ({i})"
            if candidate.strip().lower() not in existing:
                return candidate
            i += 1
            if i > 1000:
                # Defensive cap; suffix mode loops 1000 times means
                # something is wrong and the end user deserves a
                # fall-through to duplicate semantics.
                return proposed_name
    # Unknown collision_handling falls back to duplicate so the
    # end user's playlist still lands.
    return proposed_name


# ── Per-run config resolver ────────────────────────────────────────────────


def transform_playlists_for_restore(
    playlists: List[Dict[str, Any]],
    *,
    config: MixedMediaConfig,
    per_user_configs: Optional[Dict[str, MixedMediaConfig]] = None,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """Walk the list of playlist rows that the restore engine is
    about to process and apply the mixed-media strategy to each.

    Returns a NEW list of playlist rows:

    * Non-mixed playlists pass through unchanged.
    * Skipped playlists are dropped from the output.
    * Dominant playlists are returned with their items filtered.
    * Split playlists are returned as N rows with suffixed names +
      filtered items.

    Each row is a snapshot-style dict with at minimum ``name`` +
    ``items`` keys; every other key (smart_filter_json, user, etc.)
    is preserved verbatim on the output rows.

    ``per_user_configs`` lets the caller scope a per-user override:
    when a playlist row carries a ``user`` field that matches a key
    in the map, that user's config is used instead of the run-level
    config. Aligned with the Plan's per-user mixed-media setting."""
    logger = logger or log
    out: List[Dict[str, Any]] = []
    for row in playlists or []:
        if row.get("is_smart"):
            # Smart playlists are skipped by the restore engine anyway
            # (criteria don't port); pass-through so the existing
            # smart-skip log fires.
            out.append(row)
            continue

        user_key = str(row.get("user") or "").strip()
        active_config = config
        if per_user_configs and user_key in per_user_configs:
            active_config = per_user_configs[user_key]

        items = list(row.get("items") or [])
        cls = classify(items, threshold=active_config.dominance_threshold)
        outputs = apply_strategy(
            playlist_name=str(row.get("name") or ""),
            classification=cls,
            config=active_config,
            logger=logger,
        )
        if not outputs:
            # Strategy decided to skip the playlist entirely.
            continue
        for out_directive in outputs:
            new_row = dict(row)
            new_row["name"] = out_directive.name
            new_row["items"] = list(out_directive.items)
            # Tag for downstream logging / debugging.
            new_row["_mixed_media_reason"] = out_directive.reason
            new_row["_mixed_media_family"] = out_directive.family
            if out_directive.target_libraries_hint is not None:
                new_row["_mixed_media_libraries_hint"] = (
                    out_directive.target_libraries_hint
                )
            out.append(new_row)
    return out


def resolve_config_chain(
    *,
    per_user_overrides: Optional[Dict[str, Any]] = None,
    per_run_overrides: Optional[Dict[str, Any]] = None,
    global_settings: Optional[Dict[str, Any]] = None,
    tunable_fallbacks: Optional[Dict[str, Any]] = None,
) -> MixedMediaConfig:
    """Resolve a final :class:`MixedMediaConfig` by walking the
    end user's override chain in priority order:

    1. per_user_overrides (highest; from cross_platform_resolutions)
    2. per_run_overrides (from the submit body)
    3. global_settings (from server_data/settings.json)
    4. tunable_fallbacks (from services/tunables.py defaults)

    Each layer is a flat dict with the same key names as the
    config fields. ``None`` values are treated as "inherit"; only
    non-None values override the next layer."""

    def _pick(field_name: str, default: Any) -> Any:
        for layer in (
            per_user_overrides, per_run_overrides,
            global_settings, tunable_fallbacks,
        ):
            if layer is None:
                continue
            v = layer.get(field_name)
            if v is not None:
                return v
        return default

    return MixedMediaConfig(
        behavior=str(_pick("behavior", "skip")),
        dominance_threshold=float(_pick("dominance_threshold", 0.60)),
        video_routing=str(_pick("video_routing", "library_agnostic")),
        logging=str(_pick("logging", "full")),
        collision_handling=str(_pick("collision_handling", "duplicate")),
    )
