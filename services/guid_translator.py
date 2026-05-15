"""
GUID normalization (v0.12.0 - Feature 3 foundation).

Plex emits item GUIDs in several historical formats depending on which
metadata agent was active when the item was matched. The two shapes
that matter in practice:

* **Modern agent format**, used since the 2020-ish Plex agent rework:
      ``imdb://tt0133093``
      ``tvdb://121361``
      ``tmdb://603``
      ``musicbrainz://b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d``
  Clean, namespaced, directly usable as a database key.

* **Legacy "Plex agent" format**, used by older agents and still
  present on items that haven't been re-matched:
      ``com.plexapp.agents.imdb://tt0133093?lang=en``
      ``com.plexapp.agents.thetvdb://121361/2/3?lang=en``
      ``com.plexapp.agents.themoviedb://603?lang=en``
      ``com.plexapp.agents.musicbrainz://b10bbbfc-...``
  The information is the same; it's just wrapped in
  ``com.plexapp.agents.<name>://<id>``-with-noise.

Both forms identify the same external entity. For the v0.12.0 SQLite
data layer to do efficient cross-service lookups (and for Features 4 /
5 to bridge Plex ↔ Jellyfin ↔ Emby), every GUID must collapse to one
canonical form before it's hashed, indexed, or compared.

Why this lives in ``services/`` and not ``server/``
----------------------------------------------------
The CLI engine uses GUIDs too (resolver, snapshotter, importer). Putting
the helper in ``services/`` keeps it importable from both the CLI
path and the FastAPI server path with no engine/server dependency
direction violation.

Out of scope
------------
This module **does not** map GUIDs across providers (TVDB ↔ TMDB ↔
IMDb). That's a metadata-lookup concern handled by the resolver in
Features 4–5, not a string-rewrite concern. We only canonicalise the
*format* - the underlying ID is preserved as-is.
"""

from __future__ import annotations

import re
from typing import Iterable, List


# Each entry is ``(legacy_pattern, canonical_replacement)``. Patterns
# are anchored at the start so we don't accidentally collapse a
# substring inside an already-clean GUID like ``imdb://...``. Query-
# string suffixes (``?lang=en``, ``?language=…``) are stripped by the
# pattern so the canonical form has nothing dangling.
#
# Episode-shaped legacy GUIDs (``…/season/episode``) are normalised to
# the *series* id - the season/episode path components vary
# meaninglessly across Plex installations and would otherwise break
# the cross-server item match. The episode identity is reconstructed
# at lookup time using season+episode numbers from the surrounding
# item metadata; the database stores the bare series GUID.
_AGENT_PATTERNS: List[tuple] = [
    # imdb - id always starts with "tt" followed by digits.
    (re.compile(r"^com\.plexapp\.agents\.imdb://(tt\d+)(?:[/?].*)?$"),
     r"imdb://\1"),
    # tvdb (note: legacy uses "thetvdb"; modern Plex uses "tvdb").
    (re.compile(r"^com\.plexapp\.agents\.thetvdb://(\d+)(?:[/?].*)?$"),
     r"tvdb://\1"),
    (re.compile(r"^com\.plexapp\.agents\.tvdb://(\d+)(?:[/?].*)?$"),
     r"tvdb://\1"),
    # tmdb (note: legacy uses "themoviedb"; modern Plex uses "tmdb").
    (re.compile(r"^com\.plexapp\.agents\.themoviedb://(\d+)(?:[/?].*)?$"),
     r"tmdb://\1"),
    (re.compile(r"^com\.plexapp\.agents\.tmdb://(\d+)(?:[/?].*)?$"),
     r"tmdb://\1"),
    # musicbrainz - id is a hyphenated UUID; allow whatever after
    # the first non-id character.
    (re.compile(r"^com\.plexapp\.agents\.musicbrainz://([0-9a-fA-F\-]+)(?:[/?].*)?$"),
     r"musicbrainz://\1"),
    # local:// is a legitimate "no upstream match" marker for music
    # tracks Plex couldn't match to MusicBrainz. Strip the query
    # suffix but preserve the namespace so callers can detect the
    # unmatchable case explicitly.
    (re.compile(r"^local://(.+?)(?:\?.*)?$"),
     r"local://\1"),
]


# Already-canonical GUIDs we leave alone. The regex is intentionally
# permissive on the id portion - Plex sometimes emits ratings-style
# IDs that look unlike typical IMDb/TVDB IDs, and we'd rather pass
# them through than reject them.
_CANONICAL_RE = re.compile(
    r"^(imdb|tvdb|tmdb|musicbrainz|plex|local|jellyfin|emby)://[^?]+(?:\?.*)?$"
)


def normalize_guid(guid: str) -> str:
    """
    Canonicalise one GUID string.

    Returns the input unchanged if it doesn't match any known agent
    pattern AND doesn't look canonical. The pass-through means an
    unrecognised provider doesn't silently get dropped - it just
    flows through and a caller can decide whether to log or skip.

    Empty / non-string input returns an empty string. Trailing query
    strings (``?lang=…``) are stripped from canonical forms too so
    a value that's already 99% canonical also gets the final 1%.
    """
    if not isinstance(guid, str):
        return ""
    g = guid.strip()
    if not g:
        return ""
    # Legacy agent strings first - they're the noisy case.
    for pattern, replacement in _AGENT_PATTERNS:
        m = pattern.match(g)
        if m:
            return pattern.sub(replacement, g)
    # Already-canonical form: strip any trailing query suffix so two
    # otherwise-identical GUIDs that differ only by ``?lang=…`` hash
    # equal in the database.
    if _CANONICAL_RE.match(g):
        return g.split("?", 1)[0]
    return g


def normalize_guids(guids: Iterable[str]) -> List[str]:
    """
    Canonicalise a list of GUIDs, deduplicate while preserving the
    first-seen order, and drop empties.

    Order preservation matters because resolver tier-0 lookups in
    Feature 3+ prefer the most-specific GUID first (typically TVDB or
    IMDb on movies/episodes, MusicBrainz on tracks). The engine
    already emits GUIDs in agent-priority order; normalising
    must not reshuffle them.
    """
    seen: "dict[str, None]" = {}
    for g in guids or []:
        canon = normalize_guid(g)
        if canon and canon not in seen:
            seen[canon] = None
    return list(seen.keys())
