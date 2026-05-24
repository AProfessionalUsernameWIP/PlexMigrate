"""
Auto-match libraries between two servers.

The operator's case: server A has a library named ``Music`` containing
the same files as server B's library named ``Tunes``. We want the
restorer, library filter UI, and collection-creation paths to know
"Music → Tunes" automatically, without operator-typed mapping config.

Strategy (4 tiers, applied per source library; first tier yielding a
confident match wins):

  1. **Content overlap (GUIDs).** For each source library, collect its
     items' GUIDs from the mirror DB. For each destination library on
     the dest server, do the same. Compute Jaccard similarity
     ``|src ∩ dst| / |src ∪ dst|``. >=0.5 → confident automap.
     >=0.15 with matching library type → low-confidence automap.

  2. **Path-tail overlap.** Same shape but using the last N path
     components of each item's file_path (case-folded). Catches CD-
     ripped music where GUIDs are sparse but filenames agree.

  3. **Library-type + uniqueness.** If exactly one destination library
     has the same ``library_type`` as the source AND no Tier 1/2
     match landed for source, use that destination. Confidence ~0.6
     (the type matches but no content evidence — the operator should
     still confirm).

  4. **Normalized-name tie-break.** Strip whitespace, lowercase, fold
     spaces. ``RapidFuzz.token_set_ratio`` between source and remaining
     destination candidates. Used only to break Tier 3 ties when two
     destinations have the same library_type.

The match function NEVER writes to ``library_mapping_db`` — it just
returns suggestions. Callers (the REST endpoint, the operator UI)
decide what to persist as ``source='auto'`` or ``source='operator'``.

Caching: per-library GUID + path-tail sets are built fresh on each
call. The data lives in ``mirror_items`` / ``mirror_item_guids`` so
no Plex / Jellyfin / Emby live calls are needed; ~ms per library on
a typical install. Re-running the matcher is cheap.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from server import server_mirror_db


log = logging.getLogger("plexmigrate.services.library_mapper")


# Confidence thresholds. Tuned to be conservative on auto-apply so a
# weak match doesn't override the operator's manual workflow.
_CONFIDENCE_AUTO_APPLY = 0.50      # >= this, automap with source='auto'
_CONFIDENCE_SUGGEST_LOW = 0.15     # >= this and type match, suggest only
_TYPE_FALLBACK_CONFIDENCE = 0.60   # single-candidate type fallback
_PATH_TAIL_COMPONENTS = 3          # last 3 path segments


@dataclass(frozen=True)
class LibraryInfo:
    """A library section's identity + metadata for matching purposes."""
    server_id: str
    library_id: str
    library_name: str
    library_type: str
    item_count: int


@dataclass(frozen=True)
class MatchCandidate:
    """One (source, dest) pair scored by the matcher. Carried up to
    the REST layer so the UI can render the WHY of a suggestion."""
    source_library: LibraryInfo
    dest_library: LibraryInfo
    confidence: float
    tier: str
    # Cardinalities used to compute the confidence — surfaced so the
    # operator can read "4847 of 4920 GUIDs shared" in the UI.
    src_size: int
    dst_size: int
    overlap_size: int


@dataclass
class MatchResult:
    """Per-source-library best match (or None when nothing crosses
    threshold). Plus the runner-up so the UI can show alternatives."""
    source_library: LibraryInfo
    best: Optional[MatchCandidate] = None
    alternatives: List[MatchCandidate] = field(default_factory=list)


# ── Mirror-DB readers (kept here so callers don't depend on
#    server_mirror_db's schema directly) ─────────────────────────────

def _list_libraries_for_server(server_id: str) -> List[LibraryInfo]:
    """Return one ``LibraryInfo`` per section in the mirror DB for
    ``server_id``. ``library_name`` falls back to the section_id when
    the mirror doesn't carry a name. ``item_count`` is read from the
    matching ``mirror_items`` rows."""
    try:
        conn = server_mirror_db.get_connection()
    except RuntimeError:
        return []
    try:
        rows = conn.execute(
            # ``mirror_library_sections`` calls the friendly column
            # ``name`` (not ``section_name``); join to mirror_items
            # for the item count so the UI can render
            # "Music (4,920 items)" without a second query.
            "SELECT s.section_id, s.name AS section_name, "
            "       s.section_type, "
            "       COUNT(i.rating_key) AS item_count "
            "FROM mirror_library_sections s "
            "LEFT JOIN mirror_items i "
            "  ON i.server_id = s.server_id "
            " AND i.section_id = s.section_id "
            "WHERE s.server_id = ? "
            "GROUP BY s.section_id, s.name, s.section_type "
            "ORDER BY s.section_id",
            (server_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("_list_libraries_for_server failed: %s", exc)
        return []
    out: List[LibraryInfo] = []
    for r in rows:
        out.append(LibraryInfo(
            server_id=server_id,
            library_id=str(r["section_id"] or ""),
            library_name=str(r["section_name"] or r["section_id"] or "?"),
            library_type=str(r["section_type"] or ""),
            item_count=int(r["item_count"] or 0),
        ))
    return out


def _collect_guids_per_library(
    server_id: str,
) -> Dict[str, Set[str]]:
    """Return ``{library_id: {guid, guid, ...}}`` for every library on
    the server. Reads from the JOIN of ``mirror_items`` x
    ``mirror_item_guids``. Empty libraries return an empty set so
    callers can still index by library_id."""
    try:
        conn = server_mirror_db.get_connection()
    except RuntimeError:
        return {}
    out: Dict[str, Set[str]] = {}
    try:
        rows = conn.execute(
            "SELECT i.section_id, g.guid "
            "FROM mirror_items i "
            "JOIN mirror_item_guids g "
            "  ON g.server_id = i.server_id "
            " AND g.rating_key = i.rating_key "
            "WHERE i.server_id = ? AND g.guid != ''",
            (server_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("_collect_guids_per_library failed: %s", exc)
        return {}
    for r in rows:
        sec = str(r["section_id"] or "")
        if not sec:
            continue
        out.setdefault(sec, set()).add(str(r["guid"]))
    return out


def _collect_path_tails_per_library(
    server_id: str,
    *,
    components: int = _PATH_TAIL_COMPONENTS,
) -> Dict[str, Set[str]]:
    """Return ``{library_id: {tail, tail, ...}}``. Tail = the last
    ``components`` path segments of ``file_path``, lowercased + slash-
    normalised so cross-OS paths still match. Items without a
    ``file_path`` are ignored (the GUID tier should have caught those
    or they're directories / smart-only entities)."""
    try:
        conn = server_mirror_db.get_connection()
    except RuntimeError:
        return {}
    out: Dict[str, Set[str]] = {}
    try:
        rows = conn.execute(
            "SELECT section_id, file_path FROM mirror_items "
            "WHERE server_id = ? "
            "  AND file_path IS NOT NULL AND file_path != ''",
            (server_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("_collect_path_tails_per_library failed: %s", exc)
        return {}
    for r in rows:
        sec = str(r["section_id"] or "")
        path = str(r["file_path"] or "")
        if not sec or not path:
            continue
        tail = _path_tail(path, components=components)
        if tail:
            out.setdefault(sec, set()).add(tail)
    return out


def _path_tail(path: str, *, components: int) -> str:
    """Return the last ``components`` path segments of ``path`` joined
    by ``/``, lowercased. Slashes are normalised so Windows-style
    ``\\`` and POSIX ``/`` paths produce identical tails."""
    norm = path.replace("\\", "/").lower()
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return ""
    tail_parts = parts[-components:] if len(parts) > components else parts
    return "/".join(tail_parts)


# ── Scoring ─────────────────────────────────────────────────────────


def _jaccard(a: Set[Any], b: Set[Any]) -> Tuple[float, int]:
    """Return ``(similarity, overlap_size)``. Similarity = |a∩b| / |a∪b|.
    Both sets empty → 0.0 (we don't count empty libraries as a perfect
    match against each other)."""
    if not a or not b:
        return 0.0, 0
    inter = a & b
    union = a | b
    if not union:
        return 0.0, 0
    return len(inter) / len(union), len(inter)


_NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_name(name: str) -> str:
    """Strip non-alphanumerics, lowercase. ``My Music`` → ``mymusic``.
    Used only for Tier-4 tie-breaks; the matcher relies on content
    evidence first."""
    return _NAME_NORMALIZE_RE.sub("", (name or "").lower())


def _name_similarity(a: str, b: str) -> float:
    """Crude similarity in 0.0-1.0 without depending on a third-party
    fuzzy lib. Two passes:
      * Exact equality after normalization → 1.0
      * Substring relationship → 0.7
      * Jaccard over normalized 3-grams → otherwise
    """
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.7
    ga = {na[i:i+3] for i in range(len(na) - 2)} or {na}
    gb = {nb[i:i+3] for i in range(len(nb) - 2)} or {nb}
    sim, _ = _jaccard(ga, gb)
    return sim


# ── Public matcher ──────────────────────────────────────────────────


def auto_match_libraries(
    source_server_id: str,
    dest_server_id: str,
) -> List[MatchResult]:
    """Score every source library on ``source_server_id`` against every
    library on ``dest_server_id``. Returns one :class:`MatchResult`
    per source library, with ``.best`` set to the top candidate (or
    None when nothing crossed the suggest threshold) and
    ``.alternatives`` carrying the runner-ups sorted by confidence.

    The match function is read-only — it doesn't write to
    ``library_mapping_db``. Callers (REST endpoints, the
    confirm-suggestions UI) decide what to persist.
    """
    if not source_server_id or not dest_server_id:
        return []
    src_libs = _list_libraries_for_server(source_server_id)
    dst_libs = _list_libraries_for_server(dest_server_id)
    if not src_libs:
        log.info(
            "auto_match_libraries: source server %s has no libraries "
            "in the mirror; returning empty.", source_server_id,
        )
        return []
    if not dst_libs:
        return [MatchResult(source_library=s) for s in src_libs]

    # Pre-compute GUID + path-tail fingerprints once per server.
    src_guids_map = _collect_guids_per_library(source_server_id)
    dst_guids_map = _collect_guids_per_library(dest_server_id)
    src_paths_map = _collect_path_tails_per_library(source_server_id)
    dst_paths_map = _collect_path_tails_per_library(dest_server_id)

    out: List[MatchResult] = []
    for src in src_libs:
        candidates: List[MatchCandidate] = []
        src_g = src_guids_map.get(src.library_id, set())
        src_p = src_paths_map.get(src.library_id, set())

        for dst in dst_libs:
            dst_g = dst_guids_map.get(dst.library_id, set())
            dst_p = dst_paths_map.get(dst.library_id, set())

            # Tier 1: GUID overlap.
            sim_g, ov_g = _jaccard(src_g, dst_g)
            if sim_g > 0:
                candidates.append(MatchCandidate(
                    source_library=src, dest_library=dst,
                    confidence=sim_g, tier="content",
                    src_size=len(src_g), dst_size=len(dst_g),
                    overlap_size=ov_g,
                ))
                continue  # GUID tier won; skip path tier for this pair
            # Tier 2: Path-tail overlap (only when GUID tier yielded
            # nothing for this pair).
            sim_p, ov_p = _jaccard(src_p, dst_p)
            if sim_p > 0:
                candidates.append(MatchCandidate(
                    source_library=src, dest_library=dst,
                    confidence=sim_p, tier="path",
                    src_size=len(src_p), dst_size=len(dst_p),
                    overlap_size=ov_p,
                ))

        # Tier 3 + Tier 4: type-based fallback when no content evidence.
        # Used only when the best content/path candidate is below
        # the low-confidence suggest threshold OR no candidates exist.
        best_so_far = max(candidates, key=lambda c: c.confidence, default=None)
        if best_so_far is None or best_so_far.confidence < _CONFIDENCE_SUGGEST_LOW:
            type_matches = [
                d for d in dst_libs
                if d.library_type and d.library_type == src.library_type
            ]
            if len(type_matches) == 1:
                # Unique type match — moderate confidence; the operator
                # should still glance at it but it's the right
                # default. Replaces any anemic content candidate.
                dst = type_matches[0]
                candidates.append(MatchCandidate(
                    source_library=src, dest_library=dst,
                    confidence=_TYPE_FALLBACK_CONFIDENCE, tier="type",
                    src_size=0, dst_size=dst.item_count, overlap_size=0,
                ))
            elif len(type_matches) > 1:
                # Multiple destinations of the same type — Tier 4
                # tie-break on normalized name similarity.
                scored = sorted(
                    (
                        (
                            _name_similarity(src.library_name, d.library_name),
                            d,
                        )
                        for d in type_matches
                    ),
                    key=lambda kv: kv[0],
                    reverse=True,
                )
                top_score, top_dst = scored[0]
                if top_score > 0.0:
                    # Even moderate name agreement here is a useful
                    # hint; confidence is capped at the type-fallback
                    # level so the operator's manual choice stays
                    # privileged.
                    candidates.append(MatchCandidate(
                        source_library=src, dest_library=top_dst,
                        confidence=min(_TYPE_FALLBACK_CONFIDENCE, top_score),
                        tier="name",
                        src_size=0, dst_size=top_dst.item_count,
                        overlap_size=0,
                    ))

        # Sort and pick the best + alternatives (top 3 minus winner).
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        best: Optional[MatchCandidate] = None
        alts: List[MatchCandidate] = []
        if candidates and candidates[0].confidence >= _CONFIDENCE_SUGGEST_LOW:
            best = candidates[0]
            alts = candidates[1:4]
        out.append(MatchResult(
            source_library=src, best=best, alternatives=alts,
        ))
    return out


def confidence_is_auto_apply(confidence: float) -> bool:
    """Helper for the REST layer + UI: confidence >= this threshold
    means the matcher is sure enough to persist as ``source='auto'``
    without requiring operator confirmation. Below this, the UI shows
    suggestions but doesn't save without a click."""
    return float(confidence or 0.0) >= _CONFIDENCE_AUTO_APPLY


def serialize_match_result(r: MatchResult) -> Dict[str, Any]:
    """Wire-format for a single MatchResult. The REST endpoint maps
    these onto the JSON response."""
    return {
        "source_library": _serialize_lib(r.source_library),
        "best": _serialize_candidate(r.best) if r.best else None,
        "alternatives": [
            _serialize_candidate(c) for c in r.alternatives
        ],
    }


def _serialize_lib(lib: LibraryInfo) -> Dict[str, Any]:
    return {
        "server_id":    lib.server_id,
        "library_id":   lib.library_id,
        "library_name": lib.library_name,
        "library_type": lib.library_type,
        "item_count":   lib.item_count,
    }


def _serialize_candidate(c: MatchCandidate) -> Dict[str, Any]:
    return {
        "dest_library":  _serialize_lib(c.dest_library),
        "confidence":    round(c.confidence, 4),
        "tier":          c.tier,
        "src_size":      c.src_size,
        "dst_size":      c.dst_size,
        "overlap_size":  c.overlap_size,
        "auto_apply":    confidence_is_auto_apply(c.confidence),
    }
