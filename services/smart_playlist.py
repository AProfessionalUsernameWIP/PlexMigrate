"""Smart-playlist filter model + translation.

A Plex smart playlist is a saved filter, not a fixed item list. The
filter is stored on the server as an encoded URI (``Playlist.content``)
whose tag clauses - genre / mood / style / collection / label / etc. -
reference SERVER-SPECIFIC numeric tag ids. Those ids are meaningless
on any other server.

This module turns a decoded filter into a server-agnostic
``PortableSmartFilter`` (tag clauses carry NAMES, not ids) and back.
It is deliberately small: plexapi already does the two hard ends -

  * DECODE: ``Playlist.filters()`` (plexapi's ``SmartFilterMixin
    ._parseFilters``) parses ``content`` into a nested and/or dict.
  * RE-ENCODE: ``Playlist.create(smart=True, filters=...)`` ->
    ``LibrarySection._validateFieldValueTag`` resolves a tag NAME to
    the destination server's id automatically.

So the only translation this module owns is: on the source side,
swap each tag clause's id for its name (the adapter supplies the
id->name maps); the destination side is plain pass-through because
plexapi resolves the names. There is no reverse-engineering, no
content analysis, no keyword dictionary - the filter is read, not
deduced.

The module is pure: no I/O, no plexapi import. The adapter
(`services/adapters/plex.py`) does every live call and hands this
module a `RawSmartFilter`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


# A Plex filter key is "<field><operator>" - the bare field is the
# leading run of letters; the operator suffix is everything after
# (``!`` is-not, ``>>`` / ``<<`` numeric, ``=`` exact, etc.). We keep
# the operator verbatim in the portable form and never interpret it;
# only the bare field is needed, to look up whether it is a tag field.
_BARE_FIELD_RE = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class SmartClause:
    """One leaf filter clause.

    ``field`` is the bare field name (genre / userRating / title ...).
    ``operator`` is the raw operator suffix from the Plex filter key
    ('' / '!' / '>>' / '<<' / '=' ...), kept verbatim.
    ``values`` is the clause's value list (a tag clause can carry
    several). ``value_kind`` is 'tag' (values are tag NAMES -
    server-agnostic) or 'literal' (values used verbatim: title
    strings, numbers, dates, booleans).
    ``unresolved_ids`` holds any tag id the source could not name
    (kept so the clause is not silently dropped).
    """
    field: str
    operator: str
    values: Tuple[str, ...]
    value_kind: str  # 'tag' | 'literal'
    unresolved_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SmartGroup:
    """An AND / OR group of clauses and/or nested groups."""
    match: str  # 'and' | 'or'
    children: Tuple[Union["SmartGroup", SmartClause], ...]


@dataclass(frozen=True)
class PortableSmartFilter:
    """Server-agnostic representation of a smart playlist's filter.

    Carries no server-specific ids and no raw ``content`` URI, so it
    round-trips across servers and serialises cleanly for the
    "export portable filter" action.
    """
    library_type: str                       # source section type
    library_name: str                       # source section name
    libtype: str                            # filtered leaf type
    root: Optional[Union[SmartGroup, SmartClause]]
    sort: Tuple[str, ...] = ()
    limit: Optional[int] = None
    # Tag ids in the source filter that had no name on the source
    # server (rare - a dangling tag). Surfaced as a warning.
    unresolved_source_ids: Tuple[str, ...] = ()


@dataclass
class RawSmartFilter:
    """What the adapter hands :func:`to_portable`. All plexapi access
    happens in the adapter; this is plain data.

    ``decoded`` is the dict from ``Playlist.filters()`` (keys:
    ``libtype``, ``sort``, ``limit``, ``filters``).
    ``field_types`` maps a bare field name to its plexapi
    ``FilteringFilter.filterType`` ('tag' / 'integer' / 'string' /
    'boolean' / 'date'). ``tag_choices`` maps a bare tag-field name
    to that field's ``{id: name}`` choices on the source server.
    """
    section_name: str
    section_type: str
    decoded: Dict[str, Any]
    field_types: Dict[str, str] = field(default_factory=dict)
    tag_choices: Dict[str, Dict[str, str]] = field(default_factory=dict)
    playlist_name: str = ""


def _bare_field(key: str) -> Tuple[str, str]:
    """Split a Plex filter key into (bare_field, operator_suffix)."""
    m = _BARE_FIELD_RE.match(key or "")
    if not m:
        return key, ""
    return m.group(0), key[m.end():]


def _split_tag_values(value: Any) -> List[str]:
    """A tag clause value can be a single id or a comma-joined list."""
    if isinstance(value, (list, tuple)):
        flat: List[str] = []
        for v in value:
            flat.extend(_split_tag_values(v))
        return flat
    s = str(value if value is not None else "")
    return [p for p in (part.strip() for part in s.split(",")) if p]


def referenced_tag_fields(
    decoded: Dict[str, Any], field_types: Dict[str, str],
) -> List[str]:
    """The bare tag-field names referenced anywhere in a decoded
    filter tree, in first-seen order. The adapter fetches id->name
    choices only for these, not for every tag field the section
    exposes (each is a live API round-trip)."""
    out: List[str] = []

    def _walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "and" in node or "or" in node:
            grp = "and" if "and" in node else "or"
            for child in node.get(grp) or []:
                _walk(child)
            return
        if len(node) == 1:
            (k, _v), = node.items()
            bare, _op = _bare_field(k)
            if (field_types.get(bare) or "").lower() == "tag" and bare not in out:
                out.append(bare)

    _walk(decoded.get("filters"))
    return out


def _portable_clause(
    key: str, value: Any, raw: RawSmartFilter,
) -> Tuple[SmartClause, List[str]]:
    """Convert one ``{key: value}`` filter clause to a SmartClause.
    Returns the clause + the list of tag ids that could not be
    resolved to a name on the source."""
    bare, operator = _bare_field(key)
    ftype = (raw.field_types.get(bare) or "").lower()
    if ftype == "tag":
        id_to_name = raw.tag_choices.get(bare) or {}
        names: List[str] = []
        unresolved: List[str] = []
        for tag_id in _split_tag_values(value):
            name = id_to_name.get(tag_id) or id_to_name.get(str(tag_id))
            if name:
                names.append(name)
            else:
                # No name on the source: keep the id so the clause is
                # not silently dropped, and flag it.
                names.append(tag_id)
                unresolved.append(tag_id)
        return (
            SmartClause(
                field=bare, operator=operator,
                values=tuple(names), value_kind="tag",
                unresolved_ids=tuple(unresolved),
            ),
            unresolved,
        )
    # Literal field (title / userRating / year / addedAt / ...):
    # the value is portable verbatim.
    return (
        SmartClause(
            field=bare, operator=operator,
            values=tuple(str(v) for v in _split_tag_values(value)) or ("",),
            value_kind="literal",
        ),
        [],
    )


def _portable_node(
    node: Any, raw: RawSmartFilter, unresolved: List[str],
) -> Optional[Union[SmartGroup, SmartClause]]:
    """Recursively convert a decoded filter node (an and/or group or a
    single clause) into the portable tree."""
    if node is None:
        return None
    if isinstance(node, dict) and ("and" in node or "or" in node):
        match = "and" if "and" in node else "or"
        children = []
        for child in node.get(match) or []:
            converted = _portable_node(child, raw, unresolved)
            if converted is not None:
                children.append(converted)
        return SmartGroup(match=match, children=tuple(children))
    if isinstance(node, dict) and len(node) == 1:
        (key, value), = node.items()
        clause, clause_unresolved = _portable_clause(key, value, raw)
        unresolved.extend(clause_unresolved)
        return clause
    # Unexpected shape - skip rather than mistranslate.
    return None


def to_portable(raw: RawSmartFilter) -> PortableSmartFilter:
    """Translate a decoded source-server filter into a server-agnostic
    :class:`PortableSmartFilter`. Tag-field ids become names; literal
    fields pass through; unresolved tag ids are collected as a
    warning."""
    decoded = raw.decoded or {}
    unresolved: List[str] = []
    root = _portable_node(decoded.get("filters"), raw, unresolved)
    sort_raw = decoded.get("sort")
    if isinstance(sort_raw, str):
        sort = tuple(s for s in sort_raw.split(",") if s)
    elif isinstance(sort_raw, (list, tuple)):
        sort = tuple(str(s) for s in sort_raw if s)
    else:
        sort = ()
    limit = decoded.get("limit")
    try:
        limit_int = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit_int = None
    return PortableSmartFilter(
        library_type=raw.section_type,
        library_name=raw.section_name,
        libtype=str(decoded.get("libtype") or raw.section_type or ""),
        root=root,
        sort=sort,
        limit=limit_int,
        unresolved_source_ids=tuple(dict.fromkeys(unresolved)),
    )


# ── Re-encode (destination side) ─────────────────────────────────────────────

def _clause_to_plex(clause: SmartClause) -> Dict[str, Any]:
    """One SmartClause -> a plexapi advanced-filter `{key: value}`.
    The key is `field + operator` (the same shape plexapi's
    _parseFilters produced); tag values are NAMES, which plexapi's
    _validateFieldValueTag resolves to the destination server's id."""
    key = clause.field + clause.operator
    vals = list(clause.values)
    return {key: vals[0] if len(vals) == 1 else vals}


def _node_to_plex(
    node: Optional[Union[SmartGroup, SmartClause]],
) -> Optional[Dict[str, Any]]:
    if node is None:
        return None
    if isinstance(node, SmartClause):
        return _clause_to_plex(node)
    children = [
        c for c in (_node_to_plex(ch) for ch in node.children)
        if c is not None
    ]
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    return {node.match: children}


def from_portable(portable: PortableSmartFilter) -> Dict[str, Any]:
    """Convert a :class:`PortableSmartFilter` into the keyword
    arguments for plexapi's ``Playlist.create(smart=True, ...)``:
    ``{libtype, filters, sort, limit}``. Tag clause values are NAMES;
    plexapi resolves each to the destination server's id at create
    time, so no id translation happens here."""
    spec: Dict[str, Any] = {}
    if portable.libtype:
        spec["libtype"] = portable.libtype
    filt = _node_to_plex(portable.root)
    if filt is not None:
        spec["filters"] = filt
    if portable.sort:
        spec["sort"] = list(portable.sort)
    if portable.limit:
        spec["limit"] = portable.limit
    return spec


def unresolved_against(
    portable: PortableSmartFilter,
    dest_tag_choices: Dict[str, Dict[str, str]],
) -> List[str]:
    """Tag values in the portable filter that have no matching choice
    on the destination server - Plex would drop or ignore them. Used
    by the preflight to warn BEFORE a migration runs.

    ``dest_tag_choices`` is the destination's ``{field: {id: name}}``
    map. The match mirrors plexapi's ``_validateFieldValueTag``: a
    value resolves if it case-insensitively equals some choice's name
    or id. Returns ``"field: value"`` strings."""
    resolvable: Dict[str, set] = {}
    for fld, id_to_name in (dest_tag_choices or {}).items():
        toks: set = set()
        for tid, tname in id_to_name.items():
            toks.add(str(tid).lower())
            toks.add(str(tname).lower())
        resolvable[fld] = toks

    out: List[str] = []

    def _walk(node: Optional[Union[SmartGroup, SmartClause]]) -> None:
        if node is None:
            return
        if isinstance(node, SmartGroup):
            for child in node.children:
                _walk(child)
            return
        if node.value_kind != "tag":
            return
        toks = resolvable.get(node.field)
        for value in node.values:
            if toks is None or str(value).lower() not in toks:
                out.append(f"{node.field}: {value}")

    _walk(portable.root)
    return out


# ── Plain-language description (UI inspector) ────────────────────────────────

_OPERATOR_PHRASES = {
    "": "is",
    "=": "is",
    "!": "is not",
    "!=": "is not",
    ">>": "is greater than",
    ">>=": "is at least",
    "<<": "is less than",
    "<<=": "is at most",
    "<": "is before",
    ">": "is after",
}

# CONSOLE-13: SmartClause does not carry the field type, so `<` / `>`
# can only be rendered as "is before" / "is after" when the field is a
# known Plex date field. For any other field they mean numeric ordering
# and are rendered as "is less than" / "is greater than" instead. These
# are the date-typed fields in plexapi's smart-filter vocabulary.
_DATE_FIELDS = frozenset({
    "addedAt",
    "lastViewedAt",
    "originallyAvailableAt",
    "lastRatedAt",
    "updatedAt",
})


def _describe_clause(clause: SmartClause) -> str:
    # CONSOLE-13: render `<` / `>` as date phrasing only for date fields;
    # otherwise fall back to numeric "is less/greater than".
    if clause.operator in ("<", ">") and clause.field not in _DATE_FIELDS:
        phrase = "is less than" if clause.operator == "<" else "is greater than"
    else:
        phrase = _OPERATOR_PHRASES.get(clause.operator, clause.operator or "is")
    vals = clause.values
    if len(vals) == 1:
        joined = vals[0]
    else:
        joined = " / ".join(vals)
    return f"{clause.field} {phrase} {joined}"


def _describe_node(node: Union[SmartGroup, SmartClause, None]) -> str:
    if node is None:
        return "(no filter)"
    if isinstance(node, SmartClause):
        return _describe_clause(node)
    joiner = " AND " if node.match == "and" else " OR "
    parts = [_describe_node(c) for c in node.children]
    inner = joiner.join(parts)
    return f"({inner})" if len(parts) > 1 else inner


def describe(portable: PortableSmartFilter) -> str:
    """A one-line plain-language summary of a portable filter, for the
    UI inspector ("tracks where (rating is at least 7 AND genre is
    Jazz)")."""
    body = _describe_node(portable.root)
    lead = f"{portable.libtype or 'items'} where "
    text = lead + body if portable.root is not None else f"all {portable.libtype or 'items'}"
    if portable.limit:
        text += f", limit {portable.limit}"
    if portable.sort:
        text += f", sorted by {', '.join(portable.sort)}"
    return text


# ── Serialisation (migration record + preview endpoint) ──────────────────────

def _node_to_dict(
    node: Optional[Union[SmartGroup, SmartClause]],
) -> Optional[Dict[str, Any]]:
    if node is None:
        return None
    if isinstance(node, SmartClause):
        return {
            "kind": "clause",
            "field": node.field,
            "operator": node.operator,
            "values": list(node.values),
            "value_kind": node.value_kind,
            "unresolved_ids": list(node.unresolved_ids),
        }
    return {
        "kind": "group",
        "match": node.match,
        "children": [_node_to_dict(c) for c in node.children],
    }


def to_dict(portable: PortableSmartFilter) -> Dict[str, Any]:
    """Serialise a :class:`PortableSmartFilter` to a plain JSON-able
    dict for the migration record + the preview endpoint. Each tree
    node carries a ``kind`` discriminator ('group' / 'clause') so it
    is unambiguous. The plain-language ``description`` rides along so
    the UI never has to re-derive it."""
    return {
        "library_type": portable.library_type,
        "library_name": portable.library_name,
        "libtype": portable.libtype,
        "root": _node_to_dict(portable.root),
        "sort": list(portable.sort),
        "limit": portable.limit,
        "unresolved_source_ids": list(portable.unresolved_source_ids),
        "description": describe(portable),
    }
