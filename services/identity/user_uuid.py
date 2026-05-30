"""
App-generated stable user identifier (app_user_uuid).

Every (server, user) row in ``media_db.managed_users`` and
``media_db.server_users`` carries one. The UUID is the validation
handle every cross-server identity lookup keys off; the
``user_identity_map`` table stores pairs of these UUIDs instead of
the older (server_id, user_handle) tuples.

Format
------
The canonical stored form is a 4-part string:

    <Service>-<HostNameSlug>-<server_uid>-<userkey>

Concrete example (Kai on the Plex server registered as "Jade.TV"):

    Plex-JadeTV-plex_a1b2c3d4e5f67890abcdef1234567890-a3f9c2d8

Components:

  * ``Service``       - "Plex" | "Jellyfin" | "Emby". Capitalised.
                        Pulled from the row's ``service_type`` column
                        and title-cased.

  * ``HostNameSlug``  - The server's friendly name with non-alphanumeric
                        characters stripped, case preserved, capped at
                        20 chars. Mutable: when the end user renames a
                        server, every stored UUID's HostNameSlug is
                        rewritten by :func:`server.media_db.rewrite_app_user_uuid_host_slug_for_server`.

  * ``server_uid``    - developer's existing prefixed server UID (e.g.
                        ``plex_<32 hex>``). Immutable.

  * ``userkey``       - 8 hex chars random per (server, user). Generated
                        by :func:`generate_user_key` with retry-on-UNIQUE-
                        collision at the writer site. Immutable.

The Service prefix is doubled with the leading ``<service>_`` on
``server_uid`` (e.g. ``Plex-...-plex_...-...``). That is intentional:
the leading prefix makes the UUID self-describing in raw DB reads
without needing to look up the server row.

What stays the same across a server rename: ``<Service>-...-<server_uid>-<userkey>``.
Only the HostNameSlug shifts.

What stays the same across the user's lifetime: everything. The
userkey is generated once on first insert and never changes.

Why an app-generated identifier rather than reusing ``backend_user_id``?
-----------------------------------------------------------------------
``backend_user_id`` is the backend's own user ID (Plex.tv numeric
userID for Plex rows; Jellyfin / Emby UUIDs for those backends).
Each backend assigns its own. The IDs do not cross between backends
and we do not control them. ``app_user_uuid`` is the application's
own anchor: format we choose, lifetime we control, present on every
row regardless of backend, useable as a validation handle in API
payloads and as the primary key in cross-server identity_map links.
"""

from __future__ import annotations

import re
import secrets
from typing import Dict, Optional, Tuple


# ── Generation ──────────────────────────────────────────────────────────────

# 8 hex chars = 16 ** 8 ~= 4.29 billion distinct values per server.
# Collision probability for any individual insert against existing rows
# on the same server is vanishingly small at realistic Plex Home sizes
# (a 50-user server has a 50 / 4.29B = ~1.2e-8 chance per insert).
# Writer sites still wrap with retry-on-UNIQUE for full correctness.
_USERKEY_LEN_BYTES = 4  # token_hex(4) returns 8 hex characters
_USERKEY_PATTERN = re.compile(r"^[0-9a-f]{8}$")


def generate_user_key() -> str:
    """Return a fresh 8-char lowercase-hex user key.

    The writer site at :func:`server.media_db.generate_unique_user_key`
    wraps this in a retry loop that catches the UNIQUE constraint on
    ``managed_users.app_user_uuid`` and re-generates on collision.
    """
    return secrets.token_hex(_USERKEY_LEN_BYTES)


# ── Service display name ────────────────────────────────────────────────────

_SERVICE_DISPLAY = {
    "plex":     "Plex",
    "jellyfin": "Jellyfin",
    "emby":     "Emby",
}


def service_display(service_type: str) -> str:
    """Return the capitalised display form of a service_type. Unknown
    values fall back to the literal string, capitalised, so a hand-
    rolled row with a typo does not silently produce an empty Service
    segment."""
    key = (service_type or "").strip().lower()
    return _SERVICE_DISPLAY.get(key) or (service_type or "").strip().capitalize()


# ── Host name slug ──────────────────────────────────────────────────────────

# Maximum slug length. Real Plex server names like "Jade.TV - Movies,
# TV Shows, Audio-Books, Music" would otherwise produce ~40-char
# slugs that bloat the stored UUID without adding readability.
_HOSTNAME_SLUG_MAX = 20

_SLUG_STRIP_PATTERN = re.compile(r"[^A-Za-z0-9]+")


def slugify_host_name(name: str) -> str:
    """Strip non-alphanumeric characters from ``name``, preserve case,
    and cap to 20 chars.

    Examples:
        slugify_host_name("Jade.TV") -> "JadeTV"
        slugify_host_name("Living Room") -> "LivingRoom"
        slugify_host_name("Jade.TV - Movies, TV Shows") -> "JadeTVMoviesTVShows"
        slugify_host_name("") -> "Unnamed"
        slugify_host_name(None) -> "Unnamed"

    An empty input collapses to the literal string "Unnamed" so the
    UUID format always has four populated segments. The writer site
    should treat an "Unnamed" slug as a soft signal that the end user
    should give the server a friendly name; it is not an error.
    """
    if not name:
        return "Unnamed"
    cleaned = _SLUG_STRIP_PATTERN.sub("", str(name))
    if not cleaned:
        return "Unnamed"
    return cleaned[:_HOSTNAME_SLUG_MAX]


# ── Assembly ────────────────────────────────────────────────────────────────

def build_app_user_uuid(
    *,
    service_type: str,
    host_name: str,
    server_uid: str,
    user_key: str,
) -> str:
    """Assemble the canonical 4-part stored form.

    All four arguments are required. Any whitespace-only or missing
    argument raises ``ValueError`` so the writer site catches schema
    drift early (e.g. an unprefixed legacy server_uid that would
    silently produce a malformed UUID).
    """
    service = service_display(service_type)
    if not service:
        raise ValueError("service_type is required")
    if not (server_uid or "").strip():
        raise ValueError("server_uid is required")
    if not (user_key or "").strip():
        raise ValueError("user_key is required")
    slug = slugify_host_name(host_name)
    return f"{service}-{slug}-{server_uid.strip()}-{user_key.strip()}"


# ── Parsing ─────────────────────────────────────────────────────────────────

# The server_uid portion may itself contain dashes when it is a
# legacy bare UUID (uuid4 standard form `xxxxxxxx-xxxx-...`). The
# Service segment never contains a dash (literal "Plex"/"Jellyfin"/
# "Emby"), the host_slug never contains a dash (the slugifier strips
# them), and the userkey never contains a dash (8 hex chars). So we
# can recover the four parts by anchoring on the OUTER three segments:
# Service is the first slice, host_slug the second, userkey the last,
# and server_uid is everything in between (re-joined with dashes).

_UUID_MIN_PARTS = 4


def parse_app_user_uuid(uuid: str) -> Dict[str, str]:
    """Split a stored UUID into its four parts.

    Returns a dict with keys ``service``, ``host_slug``, ``server_uid``,
    ``user_key``. Raises ``ValueError`` on a malformed string so the
    caller can decide whether to recover (e.g. surface the bad value
    in a UI error) rather than silently mis-route.

    Recovers the server_uid even when it contains dashes (legacy
    bare-UUID server ids from pre-developer installs):
    Service / host_slug / userkey are dash-free by construction; the
    server_uid is whatever sits between host_slug and userkey, joined
    back with dashes if multiple dash-separated tokens fall there.
    """
    if not isinstance(uuid, str) or not uuid.strip():
        raise ValueError("uuid must be a non-empty string")
    parts = uuid.strip().split("-")
    if len(parts) < _UUID_MIN_PARTS:
        raise ValueError(
            f"malformed app_user_uuid (expected >= 4 dash-separated parts): {uuid!r}"
        )
    service = parts[0]
    host_slug = parts[1]
    user_key = parts[-1]
    server_uid = "-".join(parts[2:-1])
    if not service or not host_slug or not server_uid or not user_key:
        raise ValueError(
            f"malformed app_user_uuid (one or more empty segments): {uuid!r}"
        )
    if not _USERKEY_PATTERN.match(user_key):
        raise ValueError(
            f"malformed app_user_uuid (user_key {user_key!r} is not 8 hex chars): {uuid!r}"
        )
    return {
        "service":    service,
        "host_slug":  host_slug,
        "server_uid": server_uid,
        "user_key":   user_key,
    }


def is_valid_app_user_uuid(uuid: str) -> bool:
    """Cheap predicate: True iff ``uuid`` parses cleanly. Never raises."""
    try:
        parse_app_user_uuid(uuid)
        return True
    except (ValueError, TypeError):
        return False


def server_uid_from_app_user_uuid(uuid: str) -> Optional[str]:
    """Return the server_uid portion of ``uuid`` or ``None`` on parse
    failure. Used by the cross-server rewriter when a server is
    renamed: every UUID whose server_uid matches the renamed server
    gets its host_slug refreshed."""
    try:
        return parse_app_user_uuid(uuid)["server_uid"]
    except (ValueError, TypeError):
        return None


def rewrite_host_slug(uuid: str, new_host_name: str) -> str:
    """Return a new UUID identical to ``uuid`` except for the host_slug
    portion, which is replaced by ``slugify_host_name(new_host_name)``.

    The server_uid + user_key portions are preserved exactly so the
    identity_map links keyed on the UUID stay valid (slug is decorative
    only; lookup matches the full string but the slug changes do not
    move row identity because the writer rewrites identity_map rows in
    lockstep).

    Raises ``ValueError`` if ``uuid`` is malformed.
    """
    parts = parse_app_user_uuid(uuid)
    return build_app_user_uuid(
        service_type=parts["service"],
        host_name=new_host_name,
        server_uid=parts["server_uid"],
        user_key=parts["user_key"],
    )
