"""
Display-name substitution for user references in log lines.

The ``log_use_display_name`` tunable (off by default) flips every
call site that runs a user reference through :func:`display_for_logging`
into "substitute the stored display_name where present" mode. The
substitution is purely cosmetic: identity resolution, identity_map
lookups, and write-time addressing all keep using the raw
``username`` (the immutable handle Plex / Jellyfin / Emby gave us).
Only the human-readable string that lands in logs and run-history
fields changes.

Why it's a tunable, not always on
---------------------------------
Some end users want the raw handle in logs because it's the same
identifier they paste into URLs, search Plex.tv for, and grep
``server_data/`` for. Others find the personalised display name
("Crystal Jean") easier to scan and don't recognise the cryptic
handle ("crystalj1"). Both audiences exist; the tunable lets each
install pick.

The substitution is a read-only lookup against
``media_db.managed_users.display_name`` for the (server_id, username)
pair. When the column is NULL or the row is missing, the helper
returns the raw username so logs always have a sensible value.
"""

from __future__ import annotations

import logging
from typing import Optional


log = logging.getLogger("plexmigrate.services.identity.user_display")


def display_for_logging(
    server_id: Optional[str],
    username: Optional[str],
) -> str:
    """Return the string that should represent ``username`` in a log
    line, run-history field, or restoration_log row.

    When the ``log_use_display_name`` tunable is true AND a non-empty
    ``display_name`` is stored on the matching ``managed_users`` row,
    return the display_name. Otherwise return the raw ``username``
    unchanged.

    Both inputs are tolerant: ``None`` or empty strings short-circuit
    to whatever was passed (typically the raw username). Failure to
    read the tunable or query media_db returns the username so a
    runtime hiccup never produces a worse log line than the legacy
    path would have.
    """
    handle = (username or "").strip()
    if not handle:
        return username or ""
    try:
        from services.tunables import log_use_display_name
        if not log_use_display_name():
            return handle
    except Exception:
        return handle
    if not server_id:
        return handle
    try:
        from server.media_db import get_managed_user
        row = get_managed_user(str(server_id), handle)
        if row is None:
            return handle
        display = (row.get("display_name") or "").strip()
        return display or handle
    except Exception:
        # Any failure (DB closed, schema not initialised, lookup
        # raised) falls back to the raw handle. Cosmetic feature
        # must not raise.
        return handle
