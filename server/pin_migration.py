"""
Cross-server PIN migration helpers (Item 3 of admin-management plan).

When a server is added or refreshed and one of its managed users
overlaps (by Plex user ID, or optionally by username string) with a
managed user on another already-registered server that has a stored
Plex Home PIN, this module surfaces a list of "would you like to
migrate the PIN" suggestions. The end user confirms in the UI, the
elevation-gated apply endpoint copies the PIN over.

The detection is pure data-layer logic (no Plex API calls); the
apply is a small wrapper around the existing
``media_db.get_managed_user_credential`` /
``media_db.set_managed_user_credential`` pair, so PINs are
decrypted on read and re-encrypted on write through the same
Fernet machinery as the rest of the managed_users surface.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional


log = logging.getLogger("plexmigrate.server.pin_migration")


def compute_pin_migration_suggestions(
    target_server_id: str,
    *,
    allow_username_fallback: bool = False,
) -> Dict[str, Any]:
    """
    Compute the list of "we noticed this user has a stored PIN on
    another server" suggestions for ``target_server_id``.

    For each managed user on the target server that does NOT have a
    stored PIN, scan every other registered server's managed_users
    rows for a row that:

      * Has a stored PIN (``has_pin`` is True), AND
      * Matches the target user by ``machine_identifier`` when both
        sides have one populated (Plex user ID equivalent), OR
      * Matches by username string IF ``allow_username_fallback`` is
        True. Off by default per the end user's decision to protect
        against the rare case of unrelated Plex.tv accounts sharing
        a managed-user name.

    Returns ``{suggestions: [...]}`` where each entry has:

      * ``target_username``
      * ``source_server_id``
      * ``source_server_name``
      * ``source_username``
      * ``match_kind``: ``"machine_id"`` or ``"username"``

    Stable ordering (target_username then source_server_id) so the
    UI can render a deterministic list.
    """
    from server import media_db, server_registry
    suggestions: List[Dict[str, Any]] = []

    target_users = media_db.list_managed_users(
        target_server_id, include_hidden=True,
    )
    # Build maps from machine_identifier and from username for the
    # users on the target that DON'T have a stored PIN. These are the
    # only candidates we'd ever offer to migrate INTO.
    target_by_machine: Dict[str, Dict[str, Any]] = {}
    target_by_username: Dict[str, Dict[str, Any]] = {}
    for u in target_users:
        if u.get("has_pin"):
            continue
        mi = u.get("machine_identifier")
        if mi:
            target_by_machine[mi] = u
        target_by_username[u["username"]] = u

    if not target_by_machine and not target_by_username:
        return {"suggestions": []}

    # Iterate over every OTHER server's users; check each for PIN +
    # match against the target maps.
    all_servers = server_registry.list_servers()
    for srv in all_servers:
        sid = srv.get("id")
        if not sid or sid == target_server_id:
            continue
        src_users = media_db.list_managed_users(sid, include_hidden=True)
        for u in src_users:
            if not u.get("has_pin"):
                continue
            mi = u.get("machine_identifier")
            matched: Optional[Dict[str, Any]] = None
            match_kind = ""
            if mi and mi in target_by_machine:
                matched = target_by_machine[mi]
                match_kind = "machine_id"
            elif allow_username_fallback and u["username"] in target_by_username:
                # Only honour username fallback when the tunable is on.
                # Skip if a machine-id match already exists for this
                # target user from a different source - the stricter
                # match always wins.
                target_for_username = target_by_username[u["username"]]
                already_machine_matched = any(
                    s["target_username"] == target_for_username["username"]
                    and s["match_kind"] == "machine_id"
                    for s in suggestions
                )
                if not already_machine_matched:
                    matched = target_for_username
                    match_kind = "username"
            if matched is None:
                continue
            suggestions.append({
                "target_username": matched["username"],
                "source_server_id": sid,
                "source_server_name": srv.get("name", ""),
                "source_username": u["username"],
                "match_kind": match_kind,
            })

    # Stable ordering for deterministic UI rendering.
    suggestions.sort(key=lambda s: (s["target_username"], s["source_server_id"]))
    return {"suggestions": suggestions}


def apply_pin_migrations(
    target_server_id: str,
    confirmed: List[Dict[str, str]],
    *,
    actor: str,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Copy each confirmed PIN from the source row to the target row.
    Returns ``{applied, skipped, errors}`` where ``applied`` is a list
    of target-usernames that received a PIN, ``skipped`` is a list of
    target-usernames that were skipped because the target already has
    a PIN (defensive: someone else applied a PIN in the meantime, or
    the end user confirmed twice). ``errors`` is a list of strings,
    one per failure.

    Each entry in ``confirmed`` must carry
    ``{target_username, source_server_id, source_username}``; entries
    missing any of those keys are reported as errors and skipped.

    Writes are additive: if the target row does not exist in
    media_db yet, the credential setter inserts it. Existing PIN
    cells are NEVER overwritten by this function (the additive
    contract from the end user's "never delete pins" constraint).
    """
    from server import media_db
    logger = logger or log
    out: Dict[str, Any] = {"applied": [], "skipped": [], "errors": []}

    for entry in confirmed:
        try:
            target_username = entry["target_username"]
            src_server_id = entry["source_server_id"]
            src_username = entry["source_username"]
        except (KeyError, TypeError):
            out["errors"].append(f"malformed migration entry: {entry!r}")
            continue

        target_row = media_db.get_managed_user(target_server_id, target_username)
        if target_row and target_row.get("has_pin"):
            out["skipped"].append(target_username)
            logger.info(
                "PIN migration: skipped %r on %r (already has PIN)",
                target_username, target_server_id,
            )
            continue

        try:
            pin_plaintext = media_db.get_managed_user_credential(
                src_server_id, src_username, "plex_home_pin",
            )
        except Exception as exc:
            out["errors"].append(
                f"{target_username}: could not read source PIN: {exc}",
            )
            continue
        if pin_plaintext is None or pin_plaintext == "":
            out["errors"].append(
                f"{target_username}: source row had no readable PIN",
            )
            continue

        try:
            # If the target row doesn't exist yet, insert it first so
            # the credential write has somewhere to land.
            if target_row is None:
                media_db.upsert_managed_user(
                    server_id=target_server_id,
                    username=target_username,
                )
            media_db.set_managed_user_credential(
                server_id=target_server_id,
                username=target_username,
                kind="plex_home_pin",
                plaintext=pin_plaintext,
            )
            out["applied"].append(target_username)
            logger.info(
                "PIN migration: applied PIN for %r from server %r to server %r (actor=%r)",
                target_username, src_server_id, target_server_id, actor,
            )
        except Exception as exc:
            out["errors"].append(f"{target_username}: write failed: {exc}")

    return out
