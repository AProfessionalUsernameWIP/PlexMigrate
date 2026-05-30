"""Shared Plex owner-identification helpers.

Two call sites enumerate Plex SystemAccounts and need to skip the
row that IS the owner so the owner doesn't get appended a second
time as a managed user:

* ``services.adapters.plex.PlexAdapter.list_users``
* ``server.server_registry.get_server_users``

A naive owner-skip (``SystemAccount.id == 1`` or
``name == myPlexAccount.username``) misses the owner whenever the
local SystemAccount has a non-1 id AND a server-side display name
that differs from the Plex.tv username - the owner then lands twice,
once as ``kind: "owner"`` and once as ``kind: "managed"``. This
module consolidates the skip logic so both call sites stay in
lockstep and recognise the owner by any of:

* ``SystemAccount.accountID`` / ``.key`` matches ``myPlexAccount.id``
  (the Plex.tv numeric userID; same human regardless of display label).
* SystemAccount.name equals the owner's email (case-insensitive).
* SystemAccount.name equals the owner's email local-part (the bit
  before ``@``) — the operator's common shortening convention.

The helper accepts the values eagerly (rather than reading off the
account object in-line) so callers can compute them once per
enumeration and the function stays cheap to call inside the loop.
"""

from __future__ import annotations

from typing import Any, Optional


def derive_owner_identifiers(account: Any) -> dict:
    """Pull every canonical identifier the owner-skip check needs off
    the plexapi ``MyPlexAccount`` object once. Returns a dict with the
    four fields the helper consumes: ``email``, ``username``,
    ``account_id``, ``email_local``.

    Defensive against attribute access failures: missing fields land
    as empty strings so a partial account object still works."""
    email = (getattr(account, "email", "") or "").strip()
    username = (getattr(account, "username", "") or "").strip()
    # plexapi exposes the Plex.tv user id under several aliases
    # depending on version — try them in order.
    account_id = ""
    for attr in ("id", "userID", "userid"):
        val = getattr(account, attr, "") or ""
        if val:
            account_id = str(val).strip()
            break
    email_local = email.split("@", 1)[0] if "@" in email else ""
    return {
        "email": email,
        "username": username,
        "account_id": account_id,
        "email_local": email_local,
    }


def is_owner_system_account(
    acct: Any,
    *,
    owner_email: str = "",
    owner_username: str = "",
    owner_account_id: str = "",
    owner_email_local: str = "",
) -> bool:
    """Return True if the supplied plexapi ``SystemAccount`` is the
    server owner.

    Five signals (any match wins, OR semantics):

    1. ``SystemAccount.id == 1`` — Plex's conventional local id for
       the owner on most installs. Most servers hit this branch.
    2. ``SystemAccount.name == owner_username`` — same-string match
       against the Plex.tv account username (rare but defensible).
    3. ``SystemAccount.accountID`` (or ``.key``, depending on plexapi
       version) equals ``owner_account_id`` — the Plex.tv numeric
       userID embedded in the SystemAccount row, which Plex.tv
       guarantees is unique per human and stable per (account, server).
       This is the strongest signal because it survives every
       cosmetic rename on either side.
    4. ``SystemAccount.name`` equals ``owner_email`` (case-
       insensitive) — fallback for installs where the local
       SystemAccount was named the owner's email outright.
    5. ``SystemAccount.name`` equals ``owner_email_local`` (the bit
       before ``@``, case-insensitive) — fallback for the common
       operator shortening convention where the local name is the
       email handle without the domain.

    Returns False when none of the signals match. False means the
    caller should treat the SystemAccount as a genuine managed user
    and emit a row.

    All five identifier args default to empty strings so a caller
    that only has some of them still gets the partial-match behaviour
    (the remaining signals are silently skipped)."""
    name = (getattr(acct, "name", "") or "").strip()
    if not name:
        return False
    # Signal 1: id == 1
    try:
        local_id = int(getattr(acct, "id", 0) or 0)
    except (TypeError, ValueError):
        local_id = 0
    if local_id == 1:
        return True
    # Signal 2: name matches username
    if owner_username and name == owner_username:
        return True
    # Signal 3: SystemAccount.accountID matches myPlexAccount.id
    if owner_account_id:
        acct_id = ""
        for attr in ("accountID", "key"):
            val = getattr(acct, attr, "") or ""
            if val:
                acct_id = str(val).strip()
                break
        if acct_id and acct_id == owner_account_id:
            return True
    # Signal 4: name matches owner email (case-insensitive)
    if owner_email and name.lower() == owner_email.lower():
        return True
    # Signal 5: name matches owner email's local-part (case-insensitive)
    if owner_email_local and name.lower() == owner_email_local.lower():
        return True
    return False


def dedupe_owner_against_managed(
    users: list,
    *,
    owner_email: str = "",
    owner_email_local: str = "",
    owner_username: str = "",
) -> list:
    """Defensive last-pass dedup: walk a list of user-shaped dicts
    (each carrying ``kind`` + ``plex_id`` + ``raw_name``) and drop
    any ``kind == "managed"`` row whose identifier collides with the
    owner row's identifier set.

    This catches edge cases where the per-account skip in
    :func:`is_owner_system_account` somehow missed (operator-set
    display name that no recognised signal caught), so the corruption
    never reaches managed_users / the restore picker / any downstream
    surface.

    The owner row itself is identified by ``kind == "owner"``; we
    don't touch its position or contents. A second-pass call after
    this function is a no-op.

    Empty input + missing owner identifiers both fall through to a
    safe no-op (returns the input list unchanged)."""
    if not users:
        return users
    # Build a case-insensitive identifier set the owner is known by.
    owner_keys = {
        s.lower().strip() for s in
        (owner_email, owner_email_local, owner_username)
        if s and s.strip()
    }
    # If the owner row is in the list, also lift its plex_id +
    # raw_name into the comparison set so downstream callers that
    # populated the owner via the {kind, plex_id, raw_name} shape
    # (server_registry path) catch their own owner row too.
    for u in users:
        if isinstance(u, dict) and u.get("kind") == "owner":
            for k in ("plex_id", "raw_name", "display_name"):
                v = (u.get(k) or "").strip().lower()
                if v:
                    owner_keys.add(v)
    if not owner_keys:
        return users
    out: list = []
    for u in users:
        if not isinstance(u, dict):
            out.append(u)
            continue
        if u.get("kind") == "managed":
            for k in ("plex_id", "raw_name", "display_name"):
                v = (u.get(k) or "").strip().lower()
                if v and v in owner_keys:
                    # Collision: drop this managed row; the owner row
                    # already represents this human.
                    break
            else:
                out.append(u)
                continue
            continue
        out.append(u)
    return out
