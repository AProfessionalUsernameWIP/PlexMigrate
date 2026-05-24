"""
Plex share-state introspection.

Two helpers that hit Plex.tv to answer questions the rest of the
account-level user discovery (``account.users()``) can't answer on
its own:

  * :func:`fetch_shared_servers` - who actually has an active share
    on a SPECIFIC server right now. The account-level users list
    returns every friend / managed user across the whole account,
    including ex-shares; the per-server shared_servers endpoint is
    the ground truth.

  * :func:`fetch_home_users_protected_map` - which Plex Home users
    have a PIN set. Distinguishes "PIN-protected, can't auth without
    the PIN" from "no longer shared" so the engine can emit the
    right log line instead of a generic warning.

Both helpers fail soft. The two consumers (the managed_users sync
flow and the engine's home-user gate) treat an empty / partial
result as "we don't know" rather than "delete the row." Worst case
the end user sees stale state until the next refresh; we never
auto-prune based on a single failed fetch.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests


log = logging.getLogger("plexmigrate.services.plex_shares")


# Plex.tv base URL. Defaults to the real plex.tv but is overridable via
# the ``PLEXMIGRATE_PLEX_TV_BASE_URL`` env var so the E2E mock can
# answer the share-state lookups without leaking calls to the live
# plex.tv. The override is consulted on every call (not cached) so a
# test harness can flip it between scenarios without restarting the
# backend. Trailing slashes are stripped to keep the joined URL shape
# stable regardless of how the env value is configured.
_DEFAULT_PLEX_TV_BASE = "https://plex.tv"


def _plex_tv_base() -> str:
    raw = (os.environ.get("PLEXMIGRATE_PLEX_TV_BASE_URL", "")
           or _DEFAULT_PLEX_TV_BASE).strip()
    return raw.rstrip("/") or _DEFAULT_PLEX_TV_BASE


# Back-compat alias for callsites that imported the constant directly.
# Kept as a module attribute so existing code reads the env value at
# import time at the very least; the helper above is the canonical
# read for new code.
_PLEX_TV_BASE = _plex_tv_base()


# ── shared_servers: who is currently shared on THIS server ──────────────────

def fetch_shared_servers(
    account: Any,
    machine_id: str,
    *,
    timeout: float = 15.0,
) -> Optional[List[Dict[str, Any]]]:
    """
    Return one entry per user who currently has an active share on
    the server identified by ``machine_id``. Format::

        {
            "username":            "<plex username>",
            "title":               "<display name, may be empty>",
            "email":               "<email, may be empty>",
            "plex_user_id":        "<numeric id>",
            "owner_id":            "<numeric owner id, the account>",
            "access_token":        "<per-share token>",
            "library_section_ids": [101, 102, ...],
        }

    Returns ``None`` (not an empty list) when the lookup itself
    failed: the network call errored, the response shape was
    unrecognised, or the account argument has no usable token. The
    caller treats None as "couldn't check, keep prior state" rather
    than "no active shares - prune everyone."

    Tries the plexapi helper first (``account.sharedServers()``) and
    falls back to a direct REST call against
    ``/api/servers/{machine_id}/shared_servers``. Falling back rather
    than relying on plexapi alone insulates us from plexapi
    version-pinning drift.

    ``title`` and ``email`` are surfaced (in addition to ``username``)
    so callers can match this entry against a local row that may have
    been keyed off a SystemAccount display name instead of the
    Plex.tv username. The two identifiers can differ - for example,
    a friend whose Plex.tv username is ``adamabuissa`` may appear as
    ``Adam abu-issa`` in ``server.systemAccounts()``. The matcher
    set in ``_refresh_share_state`` combines all three so
    either string resolves to the same shared row.
    """
    if not machine_id:
        log.debug("fetch_shared_servers: machine_id is empty; returning None")
        return None

    # Path 1: plexapi.sharedServers() if available. plexapi returns
    # SharedServer objects keyed by machineIdentifier; we filter to
    # the one we care about and pull the per-share attributes.
    try:
        method = getattr(account, "sharedServers", None)
        if callable(method):
            shared_objs = method()
            if shared_objs is not None:
                rows: List[Dict[str, Any]] = []
                for so in shared_objs:
                    so_mid = (
                        getattr(so, "machineIdentifier", "")
                        or getattr(so, "serverId", "")
                        or ""
                    )
                    if so_mid != machine_id:
                        continue
                    username = getattr(so, "username", "") or ""
                    title = getattr(so, "title", "") or ""
                    email = getattr(so, "email", "") or ""
                    # Fall back to whatever string we DO have so the
                    # ``username`` field is never empty for callers
                    # that only consume that key.
                    primary = str(username or title or email or "")
                    rows.append({
                        "username": primary,
                        "title": str(title),
                        "email": str(email),
                        "plex_user_id": str(getattr(so, "userID", "") or ""),
                        "owner_id": str(getattr(so, "ownerId", "") or ""),
                        "access_token": str(getattr(so, "accessToken", "") or ""),
                        "library_section_ids": [
                            int(s.id) for s in (getattr(so, "sections", []) or [])
                            if hasattr(s, "id")
                        ],
                    })
                return rows
    except Exception as exc:
        # plexapi method exists but raised; fall through to the REST
        # path. Log at debug so the noise stays out of normal runs.
        log.debug(
            "fetch_shared_servers: account.sharedServers() raised %s; "
            "falling back to REST",
            exc, exc_info=True,
        )

    # Path 2: direct REST. plexapi's account object exposes the
    # auth token under different attribute names depending on the
    # version; try the common shapes.
    token = (
        getattr(account, "authToken", None)
        or getattr(account, "_token", None)
        or getattr(account, "authenticationToken", None)
        or ""
    )
    if not token:
        log.warning(
            "fetch_shared_servers: account has no readable auth token; "
            "cannot fall back to REST. Returning None."
        )
        return None

    url = f"{_plex_tv_base()}/api/servers/{machine_id}/shared_servers"
    try:
        resp = requests.get(
            url,
            headers={"X-Plex-Token": token, "Accept": "application/xml"},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("fetch_shared_servers REST call failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning(
            "fetch_shared_servers REST returned HTTP %d for machine %r",
            resp.status_code, machine_id,
        )
        return None

    # Plex returns an XML MediaContainer with <SharedServer> children.
    # ElementTree is in the stdlib; no extra dependency.
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
    except Exception:
        log.exception("fetch_shared_servers REST returned unparseable XML")
        return None

    rows = []
    for ss in root.findall(".//SharedServer"):
        section_ids: List[int] = []
        for sec in ss.findall("Section"):
            sid = sec.attrib.get("id")
            if sid and sid.isdigit():
                section_ids.append(int(sid))
        username = ss.attrib.get("username", "") or ""
        title = ss.attrib.get("title", "") or ""
        email = ss.attrib.get("email", "") or ""
        rows.append({
            "username": username or title or email,
            "title": title,
            "email": email,
            "plex_user_id": ss.attrib.get("userID", ""),
            "owner_id": ss.attrib.get("ownerId", ""),
            "access_token": ss.attrib.get("accessToken", ""),
            "library_section_ids": section_ids,
        })
    return rows


# ── friends index: enrich SharedServer entries with full identity ───────────

def fetch_friends_index(
    account: Any,
    *,
    timeout: float = 15.0,
) -> Optional[Dict[str, Dict[str, str]]]:
    """
    Return ``{plex_user_id: {"username", "title", "email"}}`` for every
    friend on the account. ``None`` on total failure; ``{}`` is a valid
    "account has no friends" result.

    Used by :func:`server.media_db._refresh_share_state` to enrich the
    matcher alias set with display names that ``/api/servers/{mid}/shared_servers``
    routinely omits. A SharedServer entry typically carries the Plex.tv
    handle only (``username="crystalj1"``); the local ``managed_users``
    row was populated from ``server.systemAccounts()`` which carries the
    display name (``"Crystal Jean"``). Without enrichment those two
    representations of the same person never match.

    We rely on ``account.users()`` (the friends list) rather than the
    sharedServers endpoint because the friends list ALWAYS carries
    ``username + title + email + id``, while SharedServer payloads are
    inconsistent. The two are joined by Plex.tv numeric ``userID`` /
    ``id``, which is stable across both surfaces.

    Best-effort. Any error returns ``None`` so the caller preserves
    prior state rather than acting on incomplete data.
    """
    method = getattr(account, "users", None)
    if not callable(method):
        log.debug("fetch_friends_index: account has no users() method")
        return None
    try:
        users = method()
    except Exception as exc:
        log.debug(
            "fetch_friends_index: account.users() raised %s; "
            "returning None.",
            exc, exc_info=True,
        )
        return None
    if users is None:
        return None
    index: Dict[str, Dict[str, str]] = {}
    for u in users:
        uid = str(getattr(u, "id", "") or "").strip()
        if not uid:
            continue
        index[uid] = {
            "username": str(getattr(u, "username", "") or "").strip(),
            "title": str(getattr(u, "title", "") or "").strip(),
            "email": str(getattr(u, "email", "") or "").strip(),
        }
    return index


# ── home/users: which home users are PIN-protected ──────────────────────────

def fetch_home_users_protected_map(
    account: Any,
    *,
    timeout: float = 15.0,
) -> Optional[Dict[str, bool]]:
    """
    Return ``{username: is_pin_protected}`` for every Plex Home user
    on the account.

    Returns ``None`` when the lookup itself failed. Empty dict is a
    valid result: an account with no managed home users at all.

    Like :func:`fetch_shared_servers`, tries the plexapi shape first
    and falls back to the REST endpoint ``/api/home/users``. Looks at
    each user's ``protected`` attribute (XML) or field (plexapi). Plex
    sets this to 1 on home users that have a PIN configured.

    Important: ONLY ``/api/home/users`` (or plexapi's ``homeUsers()`` /
    ``home_users()`` helpers, which wrap it) is consulted. We
    deliberately do NOT fall back to ``account.users()`` - that
    endpoint returns the full friends + home-members list, and plexapi
    exposes a ``protected`` attribute on friend rows whose meaning is
    the friend's OWN home protection state (relative to their own
    server), not their relationship to ours. Using it would surface
    every PIN-protected friend as if they were one of our home users
    and ask the end user to save a PIN we'd never use.
    """
    # Path 1: plexapi-side. Only call ``homeUsers`` / ``home_users``;
    # do not fall back to ``users()``. See docstring.
    try:
        method = (
            getattr(account, "homeUsers", None)
            or getattr(account, "home_users", None)
        )
        if callable(method):
            users = method()
            if users is not None:
                out: Dict[str, bool] = {}
                for u in users:
                    # plexapi sometimes exposes ``protected`` as int
                    # (0/1) and sometimes as a python bool / string.
                    # Normalise.
                    raw = getattr(u, "protected", None)
                    if raw is None:
                        continue
                    if isinstance(raw, bool):
                        flag = raw
                    elif isinstance(raw, (int, float)):
                        flag = bool(int(raw))
                    else:
                        flag = str(raw).strip().lower() in ("1", "true", "yes")
                    name = str(
                        getattr(u, "title", "") or getattr(u, "username", "") or ""
                    )
                    if name:
                        out[name] = flag
                # Trust plexapi when at least one row had ``protected``;
                # otherwise fall through to REST so an empty plexapi
                # answer doesn't shadow a working REST endpoint.
                if out:
                    return out
    except Exception as exc:
        log.debug(
            "fetch_home_users_protected_map: plexapi path raised %s; "
            "falling back to REST",
            exc, exc_info=True,
        )

    # Path 2: direct REST. /api/home/users returns an XML
    # MediaContainer with <User> children carrying ``protected``.
    token = (
        getattr(account, "authToken", None)
        or getattr(account, "_token", None)
        or getattr(account, "authenticationToken", None)
        or ""
    )
    if not token:
        log.warning(
            "fetch_home_users_protected_map: account has no readable "
            "auth token; returning None."
        )
        return None

    url = f"{_plex_tv_base()}/api/home/users"
    try:
        resp = requests.get(
            url,
            headers={"X-Plex-Token": token, "Accept": "application/xml"},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("fetch_home_users_protected_map REST call failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning(
            "fetch_home_users_protected_map REST returned HTTP %d",
            resp.status_code,
        )
        return None

    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
    except Exception:
        log.exception(
            "fetch_home_users_protected_map REST returned unparseable XML"
        )
        return None

    out_map: Dict[str, bool] = {}
    for u in root.findall(".//User"):
        title = u.attrib.get("title", "") or u.attrib.get("username", "")
        if not title:
            continue
        raw = u.attrib.get("protected", "0")
        out_map[title] = raw.strip().lower() in ("1", "true", "yes")
    return out_map
