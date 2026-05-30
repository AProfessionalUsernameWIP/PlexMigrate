"""
Library-mapping read helper for the restorer + library-filter consumers.

The restorer matches source library names against destination library
names exactly today. ``Music`` on source vs ``Tunes`` on dest = skip.
This helper bridges that gap by consulting ``library_mappings`` for a
saved mapping (operator-confirmed or auto-suggested) and returning the
destination library name the restorer should treat the source entry as.

Operator-confirmed mappings beat exact-name matches: if the operator
explicitly mapped ``Music → Tunes`` but the destination also has a
literal ``Music`` library, the operator's choice wins.

Auto mappings beat exact-name fallback misses but defer to exact-name
when both apply: if auto suggests ``Music → Tunes`` AND the destination
has ``Music``, the exact match wins (the operator hasn't explicitly
endorsed Tunes; the literal name match is the safer default).

The lookup is read-only + cheap (one indexed SELECT per resolution).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set


log = logging.getLogger("plexmigrate.services.library_mapping.lookup")


def resolve_dest_library_name(
    *,
    source_server_id: str,
    source_library_id: str,
    source_library_name: str,
    dest_server_id: str,
    dest_library_names: Set[str],
) -> Optional[str]:
    """Decide which destination library name a source entry should
    land on.

    Resolution order (matches the numbered inline comments below):

      1. Operator-confirmed mapping (source='operator') in the
         ``library_mappings`` table. An explicit empty dest is a
         deliberate skip (returns the EXPLICIT_SKIP sentinel); a
         confirmed dest name present on the destination wins outright
         over the exact-name match.
      2. Exact name match against ``dest_library_names`` - used when
         the operator has not confirmed a mapping.
      3. Auto mapping (source='auto') in the table - used only when
         neither an operator mapping nor an exact name applies.
      4. ``None`` - caller should fall back to its legacy skip-this-
         library behaviour.

    All four args must be supplied. Empty ``source_server_id`` or
    ``dest_server_id`` short-circuits to step 1 (legacy behaviour for
    callers that don't carry server identity).
    """
    if not source_library_name:
        return None

    # Look up the saved mapping, if any. Cheap; one indexed SELECT.
    mapping = None
    if source_server_id and source_library_id and dest_server_id:
        try:
            from server import library_mapping_db
            mapping = library_mapping_db.get_mapping(
                source_server_id=source_server_id,
                source_library_id=source_library_id,
                dest_server_id=dest_server_id,
            )
        except Exception as exc:
            log.debug(
                "library_mapping lookup failed for (%s, %s, %s): %s; "
                "falling back to exact-name match only.",
                source_server_id, source_library_id, dest_server_id, exc,
            )
            mapping = None

    op_dest_name = (
        mapping["dest_library_name"]
        if mapping and mapping.get("source") == "operator"
            and mapping.get("dest_library_name")
        else None
    )
    auto_dest_name = (
        mapping["dest_library_name"]
        if mapping and mapping.get("source") == "auto"
            and mapping.get("dest_library_name")
        else None
    )

    # 1. Operator override (if explicit dest, including a deliberate
    #    skip via empty dest_library_id which we treat as None below).
    if mapping and mapping.get("source") == "operator":
        # Operator confirmed something. If they confirmed an empty
        # dest_library_id, treat as "skip" — return a sentinel that
        # the caller can recognise.
        if not (mapping.get("dest_library_id") or ""):
            log.info(
                "library mapping: operator-confirmed SKIP for "
                "(%s,%s)->%s; restorer will not import this library.",
                source_server_id, source_library_name, dest_server_id,
            )
            return ""  # sentinel: explicit-skip
        if op_dest_name and op_dest_name in dest_library_names:
            log.info(
                "library mapping: operator override "
                "%r -> %r for dest %s",
                source_library_name, op_dest_name, dest_server_id,
            )
            return op_dest_name
        # The operator confirmed a mapping but it can't be resolved to
        # a live dest library name. Two distinct causes, logged
        # separately so the operator knows which to fix.
        if not op_dest_name:
            # The mapping row carries a dest_library_id but no
            # dest_library_name - an incomplete save. Re-saving the
            # mapping repopulates the name.
            log.warning(
                "library mapping: operator mapping for %r on dest %s has "
                "a dest_library_id but no dest_library_name; cannot "
                "resolve - re-save the mapping. Falling through.",
                source_library_name, dest_server_id,
            )
        else:
            # The named dest library is gone (renamed / deleted).
            log.warning(
                "library mapping: operator override points at %r which "
                "is not present on dest %s; falling through to exact "
                "name.",
                op_dest_name, dest_server_id,
            )

    # 2. Exact name match (preferred when operator hasn't spoken).
    if source_library_name in dest_library_names:
        return source_library_name

    # 3. Auto mapping (only when exact name doesn't apply).
    if auto_dest_name and auto_dest_name in dest_library_names:
        log.info(
            "library mapping: auto match "
            "%r -> %r for dest %s (no exact-name match)",
            source_library_name, auto_dest_name, dest_server_id,
        )
        return auto_dest_name

    # 4. Nothing matched.
    return None


# Re-export the explicit-skip sentinel so callers can compare without
# magic strings. An operator who confirms ``dest_library_id=""`` for a
# source library has explicitly told us not to import it — restorer
# should treat that as different from "fell off the end" (no mapping
# + no exact name match), so a deliberate skip doesn't surface as a
# warning.
EXPLICIT_SKIP = ""


def is_explicit_skip(resolved: Optional[str]) -> bool:
    """Convenience for callers: True only when the lookup returned the
    sentinel meaning 'operator explicitly said skip'. None means
    'no mapping found, fall back to legacy behaviour'."""
    return resolved is not None and resolved == EXPLICIT_SKIP


# ── Per-library resolution outcome ──────────────────────────────────────────
# Three terminal states callers must handle. The string return is part of
# the public contract; matched against constants on the caller side.
RESOLVE_OK = "ok"            # use ``dest_name`` to route the import
RESOLVE_SKIP = "skip"         # operator-confirmed skip; do nothing for this library
RESOLVE_NO_MATCH = "no_match" # no viable destination; caller logs + skips


def resolve_for_one_library(
    *,
    source_library_name: str,
    source_library_id: str,
    source_server_id: str,
    dest_server_id: str,
    dest_library_names: Set[str],
    library_mapping_overrides: Optional[Dict[str, str]] = None,
    ignore_library_mapping: bool = False,
    logger: Optional[logging.Logger] = None,
) -> tuple:
    """One-library mapping resolution shared by the snapshot-restore path
    (``services.restore.plex_native.engine.run_restore``) and the direct-transfer
    path (``services.direct_transfer.engine.run_direct_transfer``).

    Resolution order (matches the snapshot-restore path one-for-one so
    a snapshot-then-restore and an in-memory direct transfer pick the
    SAME destination library for any given source library):

      1. Per-run ``library_mapping_overrides`` entry for this source
         library name. Empty string = explicit skip. Non-empty = use
         that destination name (must exist on destination or we warn +
         no-match).
      2. ``ignore_library_mapping=True`` OR same-server short-circuit
         (source_server_id == dest_server_id with both truthy) →
         exact-name only.
      3. ``library_mapping_lookup.resolve_dest_library_name`` (operator-
         confirmed → exact-name → auto mapping in priority order).
      4. Otherwise: ``RESOLVE_NO_MATCH``.

    Returns ``(status, dest_name)`` where ``status`` is one of
    ``RESOLVE_OK`` / ``RESOLVE_SKIP`` / ``RESOLVE_NO_MATCH``. ``dest_name``
    is the destination library title to route to when status is OK,
    otherwise None.
    """
    _log = logger or log

    # 1. Per-run override beats everything else.
    override = (library_mapping_overrides or {}).get(source_library_name)
    if override is not None:
        if override == "":
            _log.info(
                "library_mappings: per-run override SKIP for source library "
                "%r; nothing to import.", source_library_name,
            )
            return (RESOLVE_SKIP, None)
        if override in dest_library_names:
            _log.info(
                "library_mappings: per-run override routing source %r -> "
                "dest %r (does NOT persist to the saved mapping table)",
                source_library_name, override,
            )
            return (RESOLVE_OK, override)
        _log.warning(
            "library_mappings: per-run override for %r targets %r which is "
            "not present on the destination (destination has: %r). Skipping "
            "this library; check the per-run library mapping overrides on "
            "the Run Job form.",
            source_library_name, override, sorted(dest_library_names),
        )
        return (RESOLVE_NO_MATCH, None)

    # 2. ignore_library_mapping / same-server short-circuit → exact name only.
    same_server = (
        bool(source_server_id) and bool(dest_server_id)
        and source_server_id == dest_server_id
    )
    if ignore_library_mapping or same_server:
        if source_library_name in dest_library_names:
            return (RESOLVE_OK, source_library_name)
        return (RESOLVE_NO_MATCH, None)

    # 3. Saved-mapping consult (operator → exact-name → auto).
    resolved = resolve_dest_library_name(
        source_server_id=source_server_id or "",
        source_library_id=source_library_id or "",
        source_library_name=source_library_name,
        dest_server_id=dest_server_id or "",
        dest_library_names=dest_library_names,
    )
    if is_explicit_skip(resolved):
        _log.info(
            "library_mappings: operator-confirmed SKIP for source library "
            "%r; not importing.", source_library_name,
        )
        return (RESOLVE_SKIP, None)
    if resolved is None:
        return (RESOLVE_NO_MATCH, None)
    if resolved != source_library_name:
        _log.info(
            "library_mappings: routing source %r -> dest %r via saved mapping",
            source_library_name, resolved,
        )
    return (RESOLVE_OK, resolved)


def filter_libraries_via_mappings(
    *,
    source_server_id: str,
    dest_server_id: str,
    source_entries: List[Dict[str, Any]],
    dest_library_names: set,
    override_ignore_mappings: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Split snapshot payload library entries into ``keep`` + ``drop``
    based on whether each has a viable destination library.

    Same-server short-circuit (2026-05-19): when source and destination
    are the SAME server, we never consult the mapping table — the
    restorer's exact-name match against itself always succeeds, and a
    user-built J.TV → J.TV restore shouldn't need any library mapping
    to function.

    Override (2026-05-19): when ``override_ignore_mappings`` is True,
    the filter behaves as if no mappings exist (only exact-name
    matches keep an entry). Surface from the operator's per-run
    "Ignore library mapping" toggle so a power user can force a
    legacy-shape restore without deleting their saved mappings.

    Otherwise the filter consults the ``library_mappings`` table:
      * Entry has an exact-name match on dest → keep.
      * Entry has an operator-confirmed mapping → keep (caller will
        rewrite the entry's library name to the mapped dest name).
      * Entry has an auto mapping → keep.
      * Operator-confirmed explicit skip → drop.
      * No mapping AND no exact-name match → drop.

    Returns ``{"keep": [...], "drop": [{"entry":..., "reason":...}]}``.
    Caller iterates ``keep`` and logs ``drop`` for the audit trail.

    NOTE: this function never rewrites the entries — the restorer's
    existing per-entry ``resolve_dest_library_name`` call handles
    that. This filter only decides which entries are worth iterating.
    """
    if not source_entries:
        return {"keep": [], "drop": []}
    keep: List[Dict[str, Any]] = []
    drop: List[Dict[str, Any]] = []

    # Same-server: keep everything; caller's exact-name path handles it.
    if source_server_id and dest_server_id \
            and source_server_id == dest_server_id:
        return {"keep": list(source_entries), "drop": []}

    # Override: behave as if no mappings exist. Keep only exact name.
    if override_ignore_mappings:
        for entry in source_entries:
            name = str(entry.get("library") or "")
            if not name:
                drop.append({"entry": entry, "reason": "missing library name"})
                continue
            if name in dest_library_names:
                keep.append(entry)
            else:
                drop.append({
                    "entry": entry,
                    "reason": (
                        f"no destination library named {name!r} on dest "
                        f"(operator override: ignoring library mappings)"
                    ),
                })
        return {"keep": keep, "drop": drop}

    # Default path: consult mappings.
    for entry in source_entries:
        name = str(entry.get("library") or "")
        lib_id = str(entry.get("library_section_id") or "")
        if not name:
            drop.append({"entry": entry, "reason": "missing library name"})
            continue
        resolved = resolve_dest_library_name(
            source_server_id=source_server_id or "",
            source_library_id=lib_id,
            source_library_name=name,
            dest_server_id=dest_server_id or "",
            dest_library_names=dest_library_names,
        )
        if is_explicit_skip(resolved):
            drop.append({
                "entry": entry,
                "reason": "operator-confirmed skip in library mappings",
            })
            continue
        if resolved is None:
            drop.append({
                "entry": entry,
                "reason": (
                    f"no destination library matches {name!r}; "
                    "set up a Library Mapping under Servers to route this"
                ),
            })
            continue
        keep.append(entry)
    return {"keep": keep, "drop": drop}
