"""media.db managed-user records.

Managed-user rows, Fernet-encrypted credentials, live-roster sync,
Plex Home share-state, tombstones, and auth-health signals.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from ._core import _DB_LOCK, _require_conn, log
from .identity import (
    _equivalence_class_for_uuid,
    auto_link_identity_map_by_backend_user_id,
    generate_unique_app_user_uuid,
    get_managed_user_app_uuid,
    get_row_by_app_user_uuid,
    get_server_user_app_uuid,
)


# ── Managed users ───────────────────────────────────────────────────────────
#
# Per-(server, username) records of the end users / managed users
# known to each registered server. Stores both metadata (display name,
# service type, machine identifier, last_seen) AND optional encrypted
# credentials (auth token, Plex Home PIN, Emby/Jellyfin password) for
# use by the pre-flight check and downstream job runs.
#
# Two write surfaces:
#   * upsert_managed_user() - metadata only. Called by the sync helper
#     (manual + automatic) which talks to the live API.
#     Credentials are preserved across upserts.
#   * set_managed_user_credential() - one credential at a time. Plain-
#     text input is encrypted before write; empty string clears.
#     Called by the User Management write endpoint after db_admin
#     verification.
#
# Reads (list_managed_users, get_managed_user) never return plaintext
# credentials - just ``has_token`` / ``has_pin`` / ``has_password``
# booleans. Decryption is only available via the explicit
# get_managed_user_credential() helper, intended for engine code that
# needs to actually use a stored credential.

# Credential kinds, used by the per-credential helpers below. Keeping
# them as a tuple of constants rather than an Enum keeps the code base
# free of an unnecessary import elsewhere.
MANAGED_USER_CREDENTIAL_KINDS = (
    "auth_token",
    "plex_home_pin",
    "emby_easy_pin",
    "jellyfin_easy_pin",
    "service_password",
)
_CRED_COLUMN = {
    "auth_token": "auth_token_enc",
    "plex_home_pin": "plex_home_pin_enc",
    "emby_easy_pin": "emby_easy_pin_enc",
    "jellyfin_easy_pin": "jellyfin_easy_pin_enc",
    "service_password": "service_password_enc",
}

# Per-backend PIN routing. PIN-kind credential writes that propagate
# across identity_map links use this table to pick the right encrypted
# column on each linked row based on the row's ``service_type``. The
# value goes to whichever column the destination backend uses for its
# PIN-equivalent - Plex Home PIN, Emby EasyPassword, Jellyfin
# EasyPassword. A backend missing from this map (unknown / future
# backend) is silently skipped by the propagation walk.
PIN_KINDS = frozenset(("plex_home_pin", "emby_easy_pin", "jellyfin_easy_pin"))
_PIN_COLUMN_FOR_SERVICE = {
    "plex":     "plex_home_pin_enc",
    "emby":     "emby_easy_pin_enc",
    "jellyfin": "jellyfin_easy_pin_enc",
}
# Reverse map: service_type -> the credential kind whose column lives
# on that backend's row. Used by the by-username sweep router to
# remap a Plex-Home-PIN sweep into an Emby-EasyPassword write when
# the target row is on an Emby server, etc. Keeps a single "PIN
# value" concept routed to whatever column makes sense per row.
PIN_KIND_FOR_SERVICE = {
    "plex":     "plex_home_pin",
    "emby":     "emby_easy_pin",
    "jellyfin": "jellyfin_easy_pin",
}
# Internal alias preserved for back-compat with the propagation helper
# (older code paths import the private name).
_PIN_KINDS = PIN_KINDS


def _row_to_managed_user(
    row: sqlite3.Row,
    *,
    global_set: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Public-shape managed-user dict. Never includes plaintext creds.

    ``hidden_scope`` summarises the tombstone state for the UI: 'none'
    when visible, 'server' when this specific (server, username) is
    hidden, 'global' when the username is hidden everywhere. The
    server-scope flag wins ties only when no global tombstone applies.
    Pass ``global_set`` to avoid a per-row lookup against
    ``global_tombstones`` when iterating a list.
    """
    username = row["username"]
    globally_hidden = (
        (global_set is not None and username in global_set)
        or (global_set is None and is_globally_tombstoned(username))
    )
    server_hidden = bool(row["tombstoned"])
    if globally_hidden:
        hidden_scope = "global"
    elif server_hidden:
        hidden_scope = "server"
    else:
        hidden_scope = "none"
    # Share-state columns (migration v9). Rows registered before v9
    # default to active_share=1 / is_pin_protected=0, and have NULL
    # refreshed_at. ``keys`` lookup handles the not-yet-migrated case
    # defensively; production code paths use the migrated columns.
    try:
        active_share = bool(row["active_share"])
    except (IndexError, KeyError):
        active_share = True
    try:
        is_pin_protected = bool(row["is_pin_protected"])
    except (IndexError, KeyError):
        is_pin_protected = False
    try:
        shared_state_refreshed_at = row["shared_state_refreshed_at"]
    except (IndexError, KeyError):
        shared_state_refreshed_at = None
    # Migration v10. Canonical per-user identifier (Plex.tv numeric
    # userID for ``service_type == 'plex'`` rows; the equivalent
    # backend-native id for Jellyfin / Emby once those adapters land).
    # Null on rows that pre-date v10 or that haven't yet resolved via
    # the share-state refresh path.
    try:
        backend_user_id = row["backend_user_id"]
    except (IndexError, KeyError):
        backend_user_id = None
    # Migration v12. App-generated stable user identifier
    # (USER-MGMT-IDENTITY-AUDIT follow-up). Null on rows that pre-date
    # v12 or that the boot-time backfill has not yet processed; the
    # resolution helper falls back to backend_user_id + username
    # matching when this is None.
    try:
        app_user_uuid = row["app_user_uuid"]
    except (IndexError, KeyError):
        app_user_uuid = None
    # Migration v16 columns (multi-backend PIN). Defensive: rows
    # SELECTed via an older column list won't carry these keys, so
    # fall back to False rather than KeyError. Production SELECTs go
    # through ``_MANAGED_USER_COLUMNS`` which already includes them.
    try:
        has_emby_pin = bool(row["emby_easy_pin_enc"])
    except (IndexError, KeyError):
        has_emby_pin = False
    try:
        has_jellyfin_pin = bool(row["jellyfin_easy_pin_enc"])
    except (IndexError, KeyError):
        has_jellyfin_pin = False
    # Migration v17 auth-health signals. Defensive against pre-v17
    # rows / older SELECT lists.
    try:
        last_auth_status = row["last_auth_status"] or "unknown"
    except (IndexError, KeyError):
        last_auth_status = "unknown"
    try:
        last_auth_checked_at = row["last_auth_checked_at"]
    except (IndexError, KeyError):
        last_auth_checked_at = None
    try:
        consecutive_auth_failures = int(row["consecutive_auth_failures"] or 0)
    except (IndexError, KeyError):
        consecutive_auth_failures = 0
    return {
        "id": int(row["id"]),
        "server_id": row["server_id"],
        "username": username,
        "display_name": row["display_name"],
        "service_type": row["service_type"],
        "kind": row["kind"],
        "machine_identifier": row["machine_identifier"],
        "has_token": bool(row["auth_token_enc"]),
        "has_pin": bool(row["plex_home_pin_enc"]),
        "has_emby_pin": has_emby_pin,
        "has_jellyfin_pin": has_jellyfin_pin,
        "has_password": bool(row["service_password_enc"]),
        "last_seen": row["last_seen"],
        "tombstoned": server_hidden,
        "hidden_scope": hidden_scope,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # Share-state. UI consumers render greyed-out
        # rows for ``active_share=False`` and a PIN badge for
        # ``is_pin_protected=True``. ``shared_state_refreshed_at`` is
        # surfaced in tooltips so the end user knows how fresh the
        # determination is. ``None`` means "never refreshed" - the
        # next sync will populate it.
        "active_share": active_share,
        "is_pin_protected": is_pin_protected,
        "shared_state_refreshed_at": shared_state_refreshed_at,
        "backend_user_id": backend_user_id,
        "app_user_uuid": app_user_uuid,
        "last_auth_status": last_auth_status,
        "last_auth_checked_at": last_auth_checked_at,
        "consecutive_auth_failures": consecutive_auth_failures,
    }


_MANAGED_USER_COLUMNS = (
    "id, server_id, username, display_name, service_type, kind, "
    "machine_identifier, auth_token_enc, plex_home_pin_enc, "
    "emby_easy_pin_enc, jellyfin_easy_pin_enc, "
    "service_password_enc, last_seen, tombstoned, created_at, updated_at, "
    "active_share, is_pin_protected, shared_state_refreshed_at, "
    "backend_user_id, app_user_uuid, "
    # Auth-health signals written by
    # services.user_activity_filter.record_auth_result.
    "last_auth_status, last_auth_checked_at, consecutive_auth_failures"
)


def list_managed_users(
    server_id: str,
    *,
    include_hidden: bool = False,
) -> List[Dict[str, Any]]:
    """
    Return every managed-user row for one server, oldest first.

    ``include_hidden=False`` (default) filters out rows that are
    tombstoned per-server AND any row whose username is globally
    tombstoned - matches the JobFormPanel picker contract (never
    show a hidden user as selectable). ``include_hidden=True`` is
    used by the User Management 'Show hidden' toggle and by the
    ServersPanel diff effect so the diff correctly excludes
    tombstoned users from 'newly detected' alerts.
    """
    conn = _require_conn()
    rows = conn.execute(
        f"SELECT {_MANAGED_USER_COLUMNS} "
        "FROM managed_users WHERE server_id = ? "
        # owner first so the JobFormPanel picker shows it on top of
        # the picklist - matches the live-API ordering it replaces.
        "ORDER BY CASE kind WHEN 'owner' THEN 0 ELSE 1 END, username ASC",
        (server_id,),
    ).fetchall()
    global_set = list_global_tombstone_usernames()
    out = [_row_to_managed_user(r, global_set=global_set) for r in rows]
    if include_hidden:
        return out
    return [u for u in out if u["hidden_scope"] == "none"]


def get_managed_user(server_id: str, username: str) -> Optional[Dict[str, Any]]:
    """Single-row lookup by (server_id, username). Returns ``None`` if absent."""
    conn = _require_conn()
    row = conn.execute(
        f"SELECT {_MANAGED_USER_COLUMNS} "
        "FROM managed_users WHERE server_id = ? AND username = ?",
        (server_id, username),
    ).fetchone()
    if row is None:
        return None
    return _row_to_managed_user(row)


def upsert_managed_user(
    *,
    server_id: str,
    username: str,
    display_name: Optional[str] = None,
    service_type: str = "plex",
    kind: str = "managed",
    machine_identifier: Optional[str] = None,
    last_seen: Optional[float] = None,
    backend_user_id: Optional[str] = None,
    source_user_handle: Optional[str] = None,
    created_via_user_creation: bool = False,
) -> Dict[str, Any]:
    """
    Insert or update a managed-user row's metadata. Credential columns
    are NOT touched here - this is the safe path the sync helper takes
    on every server-connect probe, and preserving existing stored
    credentials across resyncs is required (otherwise a re-sync would
    silently wipe every end user-typed PIN).

    ``service_type`` is validated against the CHECK constraint at the
    DB layer; ``kind`` likewise (migration v4). The helpers
    accept the strings as-is.

    CONSOLE-17: ``backend_user_id`` (migration v10),
    ``source_user_handle`` + ``created_via_user_creation`` (migration
    v21) carry the destination-side user-creation provenance. All three
    are COALESCEd on the ON CONFLICT path so a metadata-only re-upsert
    from the sync helper (which leaves them at their defaults) never
    erases values an earlier user-creation upsert stored.

    Returns the updated row in public shape.
    """
    if not server_id:
        raise ValueError("server_id is required")
    if not username:
        raise ValueError("username is required")
    conn = _require_conn()
    now = time.time()
    # Generate a candidate app_user_uuid outside the writer lock. The
    # COALESCE on the ON CONFLICT path preserves any existing UUID;
    # the candidate is only used when the conflict picks NULL (new row
    # OR pre-backfill row).
    candidate_uuid = generate_unique_app_user_uuid(
        service_type=service_type or "plex",
        server_id=server_id,
        server_uid=server_id,
    )
    with _DB_LOCK:
        conn.execute(
            # CONSOLE-17: persist backend_user_id / source_user_handle /
            # created_via_user_creation in BOTH the INSERT and the
            # ON CONFLICT path. The two id/handle columns COALESCE so a
            # later metadata-only resync (which passes NULL) keeps the
            # stored value; created_via_user_creation uses MAX so a row
            # ever flagged by the user-creation flow stays flagged.
            """
            INSERT INTO managed_users (
                server_id, username, display_name, service_type, kind,
                machine_identifier, last_seen, app_user_uuid,
                backend_user_id, source_user_handle,
                created_via_user_creation,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, username) DO UPDATE SET
                display_name       = COALESCE(excluded.display_name, managed_users.display_name),
                service_type       = excluded.service_type,
                kind               = excluded.kind,
                machine_identifier = COALESCE(excluded.machine_identifier, managed_users.machine_identifier),
                last_seen          = COALESCE(excluded.last_seen, managed_users.last_seen),
                app_user_uuid      = COALESCE(managed_users.app_user_uuid, excluded.app_user_uuid),
                backend_user_id    = COALESCE(excluded.backend_user_id, managed_users.backend_user_id),
                source_user_handle = COALESCE(excluded.source_user_handle, managed_users.source_user_handle),
                created_via_user_creation = MAX(managed_users.created_via_user_creation, excluded.created_via_user_creation),
                updated_at         = excluded.updated_at
            """,
            (
                server_id, username, display_name, service_type, kind,
                machine_identifier, last_seen, candidate_uuid,
                backend_user_id, source_user_handle,
                1 if created_via_user_creation else 0,
                now, now,
            ),
        )
    out = get_managed_user(server_id, username)
    assert out is not None  # we just upserted
    return out


def sync_managed_users_from_live(
    server_id: str,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Sync helper. Pulls the live user list from the registered
    server and upserts it into ``managed_users``. Metadata-only -
    credential cells are preserved across syncs.

    Sets ``kind='owner'`` for the owner row and ``kind='managed'`` for
    every other entry so the JobFormPanel picker can render its
    Owner / Managed badge from DB-backed reads.

    Best-effort: errors from ``get_server_users`` are caught and
    surfaced in the return value rather than raised. Callers
    (server-connect hooks in app.py) treat sync failure as a soft
    warning and let the server-add/test path succeed regardless.

    Returns ``{"synced": int, "source_error": Optional[str], "error": Optional[str]}``.
    """
    log_ = logger or log
    # Late import to avoid a circular dependency at module load time;
    # ``server_registry`` doesn't currently import ``media_db`` but
    # keeping this localised is defensive against future cycles.
    from server import server_registry  # noqa: WPS433 (intentional local import)

    try:
        result = server_registry.get_server_users(server_id, log_)
    except ValueError as exc:
        return {"synced": 0, "source_error": None, "error": str(exc)}
    except ConnectionError as exc:
        return {"synced": 0, "source_error": None, "error": str(exc)}

    server_row = server_registry.get_server_by_id(server_id, include_token=False) or {}
    machine_id = server_row.get("machine_identifier") or None
    # Read the registered backend type so Jellyfin / Emby users get the
    # correct service_type stamped on their managed_users row. Was
    # hardcoded 'plex' when this helper landed (live API was Plex-only
    # at the time); after developer's adapter PRs every backend goes
    # through here so the hardcode mislabels every Emby + Jellyfin row
    # as Plex. The registered row's service_type column is the source of
    # truth (servers.json drives it; the CHECK constraint on
    # managed_users.service_type enforces the same three values).
    server_service_type = (server_row.get("service_type") or "plex").strip().lower()
    if server_service_type not in ("plex", "emby", "jellyfin"):
        server_service_type = "plex"  # defensive: unknown backend falls back
    now = time.time()
    # Usernames the end user has globally tombstoned never
    # get upserted. Skipping them here keeps the row count down for
    # installs with many servers and many hidden users.
    global_set = list_global_tombstone_usernames()
    raw_users = result.get("users") or []
    server_name_for_log = server_row.get("name") or server_id

    # Pull the server's admin token so we can flag any user row whose
    # stored auth_token is byte-equal to it. This catches stale
    # admin-mirror writes: the admin token must never be mirrored onto
    # user rows, because such a row looks like it has a captured token
    # (length matches a real session token) but is actually the
    # server-scoped API key in disguise, which doesn't attribute
    # UserData to the addressed user. Best-effort: a decrypt failure
    # leaves admin_token_value=None and the equality check below
    # silently returns False.
    admin_token_value: Optional[str] = None
    try:
        from server.server_registry import decrypt_server_token as _dec
        admin_token_value = _dec(server_row) or None
    except Exception:
        admin_token_value = None

    log_.info(
        "sync_managed_users_from_live: starting for %r (id=%s, service=%s, "
        "machine_id=%s); live API returned %d user(s)",
        server_name_for_log, server_id, server_service_type,
        machine_id or "?", len(raw_users),
    )
    synced = 0
    skipped_global = 0
    upserted_rows: List[Tuple[str, str, str, str]] = []
    for row in raw_users:
        raw_name = (row.get("raw_name") or "").strip()
        if not raw_name:
            continue
        if raw_name in global_set:
            skipped_global += 1
            log_.info(
                "  - %r: SKIPPED (global tombstone)", raw_name,
            )
            continue
        # ``kind`` comes straight from get_server_users (owner|managed);
        # default to managed if missing (shouldn't happen but defensive).
        kind = row.get("kind") if row.get("kind") in ("owner", "managed") else "managed"
        backend_uid = (row.get("backend_user_id") or "").strip() or "(none)"
        display_name = (row.get("display_name") or "").strip() or "(none)"
        try:
            upsert_managed_user(
                server_id=server_id,
                username=raw_name,
                display_name=(row.get("display_name") or None),
                service_type=server_service_type,
                kind=kind,
                machine_identifier=machine_id,
                last_seen=now,
            )
            synced += 1
            upserted_rows.append((raw_name, kind, backend_uid, display_name))
            # Print whether THIS row already has a per-user
            # auth_token stored, right next to the user. Lets the
            # operator see at a glance which users have a usable
            # session token and which are still missing
            # one (without crawling the DB). The actual ciphertext
            # never leaks - we only emit the boolean + decrypted
            # length when present. The capture flow runs separately
            # in capture_managed_user_tokens (only via the
            # _auto_sync_managed_users wrapper at server-add /
            # Refresh-users); this line reflects whatever state the
            # capture flow has already produced.
            #
            # Naming note: the message key is ``cred`` (not
            # ``auth_token``) because the log scrubber's regex
            # ([\w]*token[\"']?\s*[=:]) treats anything after
            # ``token=`` / ``token:`` as a credential and replaces
            # the value with ``<redacted>``, which would mask the
            # "STORED" / "NULL" / username status strings as if they
            # were real secrets. Using ``cred`` sidesteps the regex
            # entirely so the operator sees the real status text.
            try:
                stored = get_managed_user_credential(
                    server_id, raw_name, kind="auth_token",
                )
                if stored:
                    # Flag stale admin-mirror writes by byte-comparing
                    # the stored token to the server's admin token. A
                    # match here means the row carries the admin token
                    # rather than a real per-user session token.
                    if (
                        admin_token_value is not None
                        and stored == admin_token_value
                    ):
                        token_status = (
                            f"cred: STALE admin-mirror (len={len(stored)}) "
                            f"- rotate via User Management to capture a "
                            f"real per-user session"
                        )
                    else:
                        token_status = (
                            f"cred: per-user session captured "
                            f"(len={len(stored)})"
                        )
                else:
                    token_status = "cred: MISSING"
            except Exception:
                token_status = "cred: <lookup failed>"
            log_.info(
                "  - %r: upserted (kind=%s, backend_user_id=%s, "
                "display=%s, %s)",
                raw_name, kind, backend_uid, display_name, token_status,
            )
        except Exception:
            log_.exception(
                "  - %r: upsert FAILED on server %r (continuing)",
                raw_name, server_id,
            )
            continue
    # Token-presence tally across the rows we just upserted, so the
    # operator sees the bottom-line state without summing per-user
    # log lines manually. Helps answer the question "did the capture
    # sweep actually land tokens for these users?" right at the end
    # of the sync section.
    rows_with_per_user: List[str] = []
    rows_with_stale_mirror: List[str] = []
    rows_missing: List[str] = []
    for (uname, _kind, _bk, _disp) in upserted_rows:
        try:
            stored = get_managed_user_credential(
                server_id, uname, kind="auth_token",
            )
            if not stored:
                rows_missing.append(uname)
            elif admin_token_value is not None and stored == admin_token_value:
                rows_with_stale_mirror.append(uname)
            else:
                rows_with_per_user.append(uname)
        except Exception:
            rows_missing.append(uname)
    log_.info(
        "sync_managed_users_from_live: %r done — %d upserted "
        "(per-user creds: %d, stale admin-mirror: %d, missing: %d), "
        "%d skipped via global tombstone, %d total live rows",
        server_name_for_log, synced,
        len(rows_with_per_user), len(rows_with_stale_mirror),
        len(rows_missing), skipped_global, len(raw_users),
    )
    if rows_missing:
        log_.info(
            "sync_managed_users_from_live: %r users without a stored "
            "cred: %s. The capture flow runs at server-add / "
            "Refresh-users time (not on this share-state-only sync); "
            "click Refresh on the Servers tab to trigger a sweep.",
            server_name_for_log,
            ", ".join(repr(u) for u in rows_missing),
        )
    if rows_with_stale_mirror:
        log_.info(
            "sync_managed_users_from_live: %r users with STALE admin-"
            "mirror creds (from pre-2026-05-19 code, won't attribute "
            "UserData correctly): %s. Rotate each via User Management "
            "to replace with a real per-user session via "
            "AuthenticateByName.",
            server_name_for_log,
            ", ".join(repr(u) for u in rows_with_stale_mirror),
        )
    # ── Share-state cross-reference ──────────────────────────────────────────
    # After the per-row upserts above (which keep the "do we know
    # this user exists on this server" surface unchanged), refresh
    # the three Plex.tv-sourced share-state columns:
    #   * active_share        - actually has an active share on THIS server
    #   * is_pin_protected    - has a Plex Home PIN set
    #   * shared_state_refreshed_at - when we last confirmed via Plex.tv
    #
    # Best-effort. A failed fetch leaves prior state intact and a
    # warning lands on the run log; the UI surfaces refreshed_at so
    # the end user can tell whether the badge is fresh.
    #
    # Skip entirely on non-Plex backends. The helper calls
    # ``server.myPlexAccount()`` + ``shared_servers`` + the
    # ``/api/home/users`` Plex.tv endpoint - all Plex.tv-only
    # surfaces with no Emby/Jellyfin analogue. Skipping avoids a
    # noisy "myPlexAccount() failed" warning on every sync against
    # an Emby/Jellyfin server, where the call cannot succeed by
    # design.
    share_state_error: Optional[str] = None
    if server_service_type == "plex":
        try:
            _refresh_share_state(
                server_id=server_id, machine_id=machine_id, logger=log_,
            )
        except Exception as exc:  # pragma: no cover (defensive)
            log_.exception(
                "share-state cross-reference failed for server %r", server_id,
            )
            share_state_error = f"{type(exc).__name__}: {exc}"
    else:
        log_.debug(
            "sync_managed_users_from_live: skipping share-state "
            "refresh for non-Plex server %r (service=%s; "
            "myPlexAccount / shared_servers are Plex.tv-only)",
            server_name_for_log, server_service_type,
        )

    # USER-MGMT-IDENTITY-AUDIT R-2: after every sync, re-derive
    # auto_copy identity_map rows from same-(service_type,
    # backend_user_id) pairs across servers. Closes the cross-server
    # owner case (and same-human-different-username case) with zero
    # end user action. Best-effort: a failure here never blocks the
    # sync result. Idempotent so repeated calls write each pair once
    # and silently skip duplicates on subsequent runs.
    auto_link_error: Optional[str] = None
    auto_link_pairs = 0
    try:
        # Pass scope_to_server_id so the auto-link sweep's verbose
        # log lines only print for groups that include the just-
        # synced server. Cross-server pairing math still runs for
        # every group across the whole table (correct behavior for
        # identity_map maintenance); the operator just doesn't see
        # other servers' usernames in the log when they clicked
        # Sync on a specific server.
        auto_link_summary = auto_link_identity_map_by_backend_user_id(
            scope_to_server_id=server_id,
        )
        auto_link_pairs = auto_link_summary.get("pairs_written") or 0
    except Exception as exc:  # pragma: no cover (defensive)
        log_.exception(
            "auto_link_identity_map_by_backend_user_id failed for "
            "server %r; identity-map auto-derivation will retry on "
            "next sync.",
            server_id,
        )
        auto_link_error = f"{type(exc).__name__}: {exc}"

    return {
        "synced": synced,
        "skipped_global_tombstones": skipped_global,
        "source_error": result.get("error"),
        "share_state_error": share_state_error,
        "auto_link_pairs_written": auto_link_pairs,
        "auto_link_error": auto_link_error,
        "error": None,
    }


def _refresh_share_state(
    *,
    server_id: str,
    machine_id: Optional[str],
    logger: logging.Logger,
) -> None:
    """
    Cross-reference the live Plex.tv shared_servers + home/users
    endpoints against the local managed_users table for ``server_id``
    and stamp the three share-state columns
    (``active_share``, ``is_pin_protected``,
    ``shared_state_refreshed_at``).

    Resolution rules per row:

      * ``active_share=1`` when the username appears in
        ``shared_servers`` for this machine identifier, OR when the
        row is the owner row (the admin is always "shared" with
        themselves). ``active_share=0`` otherwise.
      * ``is_pin_protected=1`` when the username appears in
        ``/api/home/users`` with ``protected=1``. ``0`` otherwise.

    Fails soft. Missing machine_id, missing PlexAccount, network
    failure, or empty fetch results leave prior state intact (no
    columns updated) and we log a warning.
    """
    if not machine_id:
        logger.debug(
            "_refresh_share_state: server %r has no machine_identifier; "
            "skipping share-state refresh.", server_id,
        )
        return

    # Connect to Plex via the registered server's stored token so we
    # have an account object to introspect. We use the existing
    # connect_to_server primitive rather than building a parallel
    # auth path; failures here mean "couldn't refresh," not "prune".
    from server import server_registry
    try:
        _conn = server_registry.connect_registered_server(
            server_id, logger=logger,
        )
        srv = _conn.server
    except Exception:
        logger.warning(
            "_refresh_share_state: could not connect to server %r; "
            "share-state refresh skipped.", server_id,
        )
        return

    try:
        account = srv.myPlexAccount()
    except Exception:
        logger.warning(
            "_refresh_share_state: myPlexAccount() failed for server %r; "
            "share-state refresh skipped.", server_id,
        )
        return

    from services.identity import plex_shares
    shared = plex_shares.fetch_shared_servers(account, machine_id)
    protected_map = plex_shares.fetch_home_users_protected_map(account)
    # Friends index gives us {plex_user_id: {username, title, email}} for
    # every friend on the account. SharedServer payloads from Plex.tv
    # routinely omit `title` / `email`; the friends list always carries
    # them. We use it both for alias enrichment (legacy path, for rows
    # not yet ID-resolved) and as a sanity cross-check for the IDs we
    # do see.
    friends_index = plex_shares.fetch_friends_index(account)

    # Owner's Plex.tv userID. Stored on the owner managed_users row for
    # Feature 5 cross-server identity links; not used for active_share
    # decisions (owner rows are unconditionally active). Best-effort
    # against several attribute names plexapi exposes across versions.
    owner_user_id = ""
    for attr in ("id", "userID", "userid"):
        val = getattr(account, attr, "") or ""
        if val:
            owner_user_id = str(val).strip()
            break

    # Defensive: if BOTH share + protected fetches failed, do not touch
    # the columns - the prior state plus a stale timestamp is more
    # honest than zeroing everything out. friends_index alone can't
    # determine active_share so we don't gate on it.
    if shared is None and protected_map is None:
        logger.warning(
            "_refresh_share_state: both shared_servers and home/users "
            "lookups failed for server %r; share-state preserved.",
            server_id,
        )
        return

    # ── Build matcher indices ───────────────────────────────────────
    # Migration v10: once a row has its ``backend_user_id``
    # populated, we match on that and ignore the alias set entirely.
    # The alias path is a backfill fallback for rows that pre-date
    # v10 or haven't yet been resolved.
    #
    #   * ``shared_by_userid`` maps Plex.tv userID -> SharedServer
    #     entry. The ID-first matcher is one dict lookup per row.
    #   * ``alias_to_userid`` maps lowercased alias string ->
    #     Plex.tv userID. When an alias hits, we both flip
    #     active_share=1 AND backfill the row's backend_user_id so
    #     subsequent refreshes use the definitive ID path.
    shared_by_userid: Dict[str, Dict[str, Any]] = {}
    alias_to_userid: Dict[str, str] = {}
    # Defensive: a SharedServer entry without a plex_user_id (older
    # plexapi shapes, sparse XML) still carries aliases we can match
    # on. Track them in ``seen_aliases`` so name matching still works
    # for the row; backfill simply can't happen in that case.
    seen_aliases: set = set()
    if shared is not None:
        for entry in shared:
            pid = (entry.get("plex_user_id") or "").strip()
            if pid:
                shared_by_userid[pid] = entry
            # Direct fields straight off the SharedServer payload.
            for key in ("username", "title", "email"):
                val = (entry.get(key) or "").strip().lower()
                if val:
                    seen_aliases.add(val)
                    if pid:
                        alias_to_userid[val] = pid
            # Enriched fields from the friends index (SharedServer
            # payloads often omit title/email; friends always carries
            # them).
            if pid and friends_index and pid in friends_index:
                friend = friends_index[pid]
                for key in ("username", "title", "email"):
                    val = (friend.get(key) or "").strip().lower()
                    if val:
                        seen_aliases.add(val)
                        alias_to_userid[val] = pid

    # PIN lookup is keyed by whatever name plexapi / REST returned for
    # the home-user row. Lowercase normalised the same way so a
    # SystemAccount display-name row resolves against a /api/home/users
    # title row.
    protected_lookup: Dict[str, bool] = {}
    if protected_map is not None:
        for k, v in protected_map.items():
            key = (k or "").strip().lower()
            if key:
                protected_lookup[key] = bool(v)

    now = time.time()
    conn = _require_conn()
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT id, username, kind, backend_user_id "
            "FROM managed_users WHERE server_id = ?",
            (server_id,),
        ).fetchall()
        for r in rows:
            uname = (r["username"] or "").strip().lower()
            kind = r["kind"] or "managed"
            existing_uid = (r["backend_user_id"] or "").strip()

            # Three-arm decision:
            #   * shared fetch failed -> leave active_share alone (None)
            #   * owner -> always active; backfill ID if missing
            #   * managed -> ID-first match, alias fallback w/ backfill
            backfill_uid: Optional[str] = None
            if kind == "owner":
                active_val: Optional[int] = 1
                if not existing_uid and owner_user_id:
                    backfill_uid = owner_user_id
            elif shared is None:
                active_val = None
            elif existing_uid:
                # Definitive ID-based match. Immune to name variants,
                # Unicode quirks, display-name drift, and same-name
                # collisions.
                active_val = 1 if existing_uid in shared_by_userid else 0
            else:
                matched_uid = alias_to_userid.get(uname)
                if matched_uid:
                    active_val = 1
                    # Persist the ID so the next refresh skips alias
                    # matching entirely for this row.
                    backfill_uid = matched_uid
                elif uname in seen_aliases:
                    # SharedServer entry carried no plex_user_id but
                    # the alias still matched. Stay active; just
                    # can't backfill.
                    active_val = 1
                else:
                    active_val = 0

            # PIN status. None means "couldn't fetch home/users";
            # leave the column alone. Friends never appear in
            # /api/home/users so they fall through to the default
            # `0` from `protected_lookup.get(uname, False)`.
            if protected_map is None:
                pin_val: Optional[int] = None
            else:
                pin_val = 1 if protected_lookup.get(uname, False) else 0

            conn.execute(
                """
                UPDATE managed_users
                SET active_share = COALESCE(?, active_share),
                    is_pin_protected = COALESCE(?, is_pin_protected),
                    shared_state_refreshed_at = ?,
                    backend_user_id = COALESCE(?, backend_user_id)
                WHERE id = ?
                """,
                (active_val, pin_val, now, backfill_uid, r["id"]),
            )


def set_managed_user_share_state(
    *,
    server_id: str,
    username: str,
    active_share: Optional[bool] = None,
    is_pin_protected: Optional[bool] = None,
    refreshed_at: Optional[float] = None,
    backend_user_id: Optional[str] = None,
) -> None:
    """
    Stamp the share-state columns on a single managed_users row.

    Each parameter is independent: ``None`` means "do not change this
    column" so the helper composes with the COALESCE-based update in
    :func:`_refresh_share_state`. ``refreshed_at`` defaults to
    ``time.time()`` so test seeds get a sensible non-NULL timestamp.
    ``backend_user_id`` (migration v10) seeds the canonical Plex.tv
    userID for tests that want to verify the ID-first matcher path.

    Public surface for test seeding and the future per-server "refresh
    shared state" path. Live sync still routes through
    :func:`sync_managed_users_from_live` -> :func:`_refresh_share_state`.
    Raises ``ValueError`` if the row isn't present.
    """
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    conn = _require_conn()
    ts = refreshed_at if refreshed_at is not None else time.time()
    a_val = None if active_share is None else (1 if active_share else 0)
    p_val = None if is_pin_protected is None else (1 if is_pin_protected else 0)
    with _DB_LOCK:
        cur = conn.execute(
            """
            UPDATE managed_users
            SET active_share = COALESCE(?, active_share),
                is_pin_protected = COALESCE(?, is_pin_protected),
                shared_state_refreshed_at = ?,
                backend_user_id = COALESCE(?, backend_user_id)
            WHERE server_id = ? AND username = ?
            """,
            (a_val, p_val, ts, backend_user_id, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"no managed_users row for server_id={server_id!r} username={username!r}"
            )


# ── Tombstones ──────────────────────────────────────────────────────────────
#
# Two scopes:
#   * Per-server: ``managed_users.tombstoned`` flag on one row. The
#     username is hidden on that specific server only; same username
#     on another server stays visible. Credentials are preserved
#     across hide/unhide cycles.
#   * Global: a row in ``global_tombstones`` keyed by username only.
#     The sync helper above skips usernames in this set, and the
#     list query filters them out regardless of the row's per-server
#     tombstoned flag.
#
# The User Management 'Hide user' modal lets the end user pick which
# scope to apply.

def set_managed_user_tombstone(
    *,
    server_id: str,
    username: str,
    tombstoned: bool,
) -> Dict[str, Any]:
    """
    Set or clear the per-server tombstone flag for one managed user.
    Credentials and other metadata are untouched. Raises ``ValueError``
    if no row matches.
    """
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            "UPDATE managed_users SET tombstoned = ?, updated_at = ? "
            "WHERE server_id = ? AND username = ?",
            (1 if tombstoned else 0, now, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
    out = get_managed_user(server_id, username)
    assert out is not None
    # M13: tombstone mutators are auditable state changes on a
    # credential-bearing table - record them like every other write.
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="managed_users",
            field="tombstoned",
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent=("set tombstone" if tombstoned else "clear tombstone"),
        )
    except Exception:
        log.exception("db_access_log emit failed for set_managed_user_tombstone")
    return out


def update_managed_user_auth_signal(
    *,
    server_id: str,
    username: str,
    result: str,
    detail: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist the outcome of one auth probe / passive 401 capture for
    a managed user. ``result`` must be one of:
      - 'ok': resets ``consecutive_auth_failures`` to 0; stamps
        ``last_auth_status='ok'`` and ``last_auth_checked_at=now``.
      - 'auth_error' / 'unreachable': increments
        ``consecutive_auth_failures``; stamps status + checked-at.
      - 'unknown': stamps ``last_auth_checked_at=now`` only; counter
        is left as-is (we didn't actually learn anything).

    Returns the row dict after the update, or None when no row
    matched (we never raise on missing-user; passive capture sites
    can call this freely without knowing whether the user is
    registered yet).

    Idempotent. Logs the write through db_access_log so an operator
    can audit.
    """
    if not server_id or not username:
        return None
    if result not in ("ok", "auth_error", "unreachable", "unknown"):
        # Caller-side validation should catch this; coerce defensively.
        log.warning(
            "update_managed_user_auth_signal: invalid result %r for "
            "%s on %s; coercing to 'unknown'",
            result, username, server_id,
        )
        result = "unknown"
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        if result == "ok":
            cur = conn.execute(
                "UPDATE managed_users SET "
                "last_auth_status = ?, last_auth_checked_at = ?, "
                "consecutive_auth_failures = 0, updated_at = ? "
                "WHERE server_id = ? AND username = ?",
                (result, now, now, server_id, username),
            )
        elif result in ("auth_error", "unreachable"):
            cur = conn.execute(
                "UPDATE managed_users SET "
                "last_auth_status = ?, last_auth_checked_at = ?, "
                "consecutive_auth_failures = consecutive_auth_failures + 1, "
                "updated_at = ? "
                "WHERE server_id = ? AND username = ?",
                (result, now, now, server_id, username),
            )
        else:  # 'unknown'
            cur = conn.execute(
                "UPDATE managed_users SET "
                "last_auth_status = ?, last_auth_checked_at = ?, "
                "updated_at = ? "
                "WHERE server_id = ? AND username = ?",
                (result, now, now, server_id, username),
            )
        if cur.rowcount == 0:
            # Passive capture site fired for a user we don't track
            # in managed_users yet. Not an error; just nothing to do.
            return None
    out = get_managed_user(server_id, username)
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="managed_users",
            field="last_auth_status",
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent=f"auth probe result={result}"
                   + (f"; {detail}" if detail else ""),
        )
    except Exception:
        log.exception(
            "db_access_log emit failed for update_managed_user_auth_signal"
        )
    return out


def reset_managed_user_auth_signal(
    *,
    server_id: str,
    username: str,
) -> Optional[Dict[str, Any]]:
    """Operator action: clear a user's auth-health counter without
    un-tombstoning anyone. Used by the per-user 'Reset counter' UI
    affordance in the User Mapping panel. Returns the row after the
    reset, or None if missing."""
    if not server_id or not username:
        return None
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            "UPDATE managed_users SET "
            "consecutive_auth_failures = 0, last_auth_status = 'unknown', "
            "updated_at = ? "
            "WHERE server_id = ? AND username = ?",
            (now, server_id, username),
        )
        if cur.rowcount == 0:
            return None
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="managed_users",
            field="consecutive_auth_failures",
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent="operator reset auth-failure counter",
        )
    except Exception:
        log.exception(
            "db_access_log emit failed for reset_managed_user_auth_signal"
        )
    return get_managed_user(server_id, username)


def add_global_tombstone(username: str) -> None:
    """
    Hide ``username`` across every registered server. Idempotent. The
    sync helper will skip this username on every future run, and
    ``list_managed_users`` filters it out from the default visible
    set. Existing per-server rows for this username stay in the DB
    (creds preserved) but are reported with ``hidden_scope='global'``.
    """
    uname = (username or "").strip()
    if not uname:
        raise ValueError("username is required")
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            "INSERT INTO global_tombstones (username, tombstoned_at) "
            "VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET tombstoned_at = excluded.tombstoned_at",
            (uname, now),
        )
    # M13: auditable - hides a user across every registered server.
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="global_tombstones",
            where={"username": uname},
            affected_rows=cur.rowcount,
            intent="add global tombstone (hide user on all servers)",
        )
    except Exception:
        log.exception("db_access_log emit failed for add_global_tombstone")


def remove_global_tombstone(username: str) -> None:
    """
    Unhide ``username`` globally. Idempotent. The username becomes
    syncable again on the next sync run.
    """
    uname = (username or "").strip()
    if not uname:
        raise ValueError("username is required")
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM global_tombstones WHERE username = ?",
            (uname,),
        )
    # Auditable - makes a hidden user syncable again.
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="global_tombstones",
            where={"username": uname},
            affected_rows=cur.rowcount,
            intent="remove global tombstone (unhide user on all servers)",
        )
    except Exception:
        log.exception("db_access_log emit failed for remove_global_tombstone")


def list_global_tombstones() -> List[Dict[str, Any]]:
    """
    Return every globally-tombstoned username with its tombstoned_at
    timestamp. Used by the User Management UI to render a 'Globally
    hidden usernames' list with per-row Unhide controls.
    """
    conn = _require_conn()
    rows = conn.execute(
        "SELECT username, tombstoned_at FROM global_tombstones "
        "ORDER BY tombstoned_at DESC"
    ).fetchall()
    return [
        {"username": r["username"], "tombstoned_at": r["tombstoned_at"]}
        for r in rows
    ]


def list_global_tombstone_usernames() -> set:
    """Fast set lookup used by the list query and sync helper."""
    conn = _require_conn()
    rows = conn.execute("SELECT username FROM global_tombstones").fetchall()
    return {r["username"] for r in rows}


def is_globally_tombstoned(username: str) -> bool:
    """Single-username probe. Cheap enough for one-off checks."""
    if not username:
        return False
    conn = _require_conn()
    row = conn.execute(
        "SELECT 1 FROM global_tombstones WHERE username = ?",
        (username,),
    ).fetchone()
    return row is not None


def set_managed_user_credential(
    *,
    server_id: str,
    username: str,
    kind: str,
    plaintext: str,
    propagate_to_linked: bool = True,
    cross_backend: bool = False,
) -> Dict[str, Any]:
    """
    Encrypt and write one credential cell. ``plaintext`` is encrypted
    via :func:`server.secrets.encrypt_str` (Fernet) before storage; an
    empty string clears the cell. ``kind`` must be one of
    :data:`MANAGED_USER_CREDENTIAL_KINDS`.

    Caller (the User Management write endpoint) is responsible for
    db_admin verification before calling this.

    Propagation: when ``propagate_to_linked`` is True (default) AND
    ``kind`` is one of the PIN kinds (``plex_home_pin`` /
    ``emby_easy_pin`` / ``jellyfin_easy_pin``), the encrypted value
    is also written to every other (server, user) row linked to this
    one via ``user_identity_map``. Same-backend linked rows are
    written unconditionally - the same human's PIN is the same on
    every server of the same backend they appear on. Cross-backend
    linked rows
    (e.g., a Plex row linked to a Jellyfin row as the same human via
    operator-curated identity_map) are skipped unless ``cross_backend``
    is True; that's the operator's opt-in "apply across backends"
    button. On a cross-backend write the destination column is chosen
    from the linked row's ``service_type`` via
    :data:`_PIN_COLUMN_FOR_SERVICE` so the value lands in whatever
    PIN-equivalent column that backend uses.

    Other credential kinds are NEVER propagated: ``auth_token`` is
    per-server (switchUser tokens), ``service_password`` is
    per-service-instance.

    Pass ``propagate_to_linked=False`` to override propagation when the
    caller explicitly wants the new credential to apply to THIS server
    only - e.g., the user-capture path that has just observed a fresh
    server-specific token and doesn't want it leaking sideways. The
    ``cross_backend`` flag is a no-op without propagation.
    """
    if kind not in _CRED_COLUMN:
        raise ValueError(
            f"Unknown credential kind {kind!r}. "
            f"Valid: {', '.join(MANAGED_USER_CREDENTIAL_KINDS)}"
        )
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    from server.secrets import encrypt_str  # local import to avoid cycle on module load

    column = _CRED_COLUMN[kind]
    encrypted = encrypt_str(plaintext or "") or None  # empty string clears
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            f"UPDATE managed_users SET {column} = ?, updated_at = ? "
            "WHERE server_id = ? AND username = ?",
            (encrypted, now, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
    # Propagate PINs across identity_map links. Outside the writer
    # lock so each per-linked-row UPDATE re-acquires the lock on its
    # own short call (avoids holding it across BFS reads). Mirrors
    # :func:`set_managed_user_display_name`.
    if propagate_to_linked and kind in _PIN_KINDS:
        try:
            from server import media_db as _md
            origin = get_managed_user(server_id, username)
            origin_service = (origin or {}).get("service_type") or ""
            _md._propagate_pin_across_links(
                origin_server_id=server_id,
                origin_username=username,
                origin_service_type=origin_service,
                encrypted_value=encrypted,
                now=now,
                cross_backend=cross_backend,
            )
        except Exception:
            log.exception(
                "set_managed_user_credential: identity-link PIN propagation "
                "failed for (%r, %r); the origin row was updated but linked "
                "rows may be stale.",
                server_id, username,
            )
    out = get_managed_user(server_id, username)
    assert out is not None
    # Audit trail. Recorded regardless of plaintext/empty so the
    # end user can also see "clear" operations.
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="managed_users",
            field=column,
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent=("clear credential" if not plaintext else f"set {kind}"),
        )
    except Exception:
        pass
    return out


def _propagate_pin_across_links(
    *,
    origin_server_id: str,
    origin_username: str,
    origin_service_type: str,
    encrypted_value: Optional[str],
    now: float,
    cross_backend: bool = False,
) -> int:
    """Walk the identity_map equivalence class for the origin row and
    write ``encrypted_value`` onto every linked managed_users row.
    Returns the number of linked rows actually updated (not counting
    the origin row, which the caller already updated).

    Routing rules:
      * Same-backend linked rows are always written. The destination
        column is the same kind the origin used (per
        :data:`_PIN_COLUMN_FOR_SERVICE`).
      * Cross-backend linked rows (different ``service_type`` from the
        origin) are written ONLY when ``cross_backend`` is True. The
        destination column is chosen from the linked row's
        ``service_type``, NOT the origin's - Plex<->Emby propagation
        writes ``plex_home_pin_enc`` on the Plex row and
        ``emby_easy_pin_enc`` on the Emby row, both holding the same
        Fernet blob.
      * Linked rows on backends with no PIN-equivalent column
        (anything outside :data:`_PIN_COLUMN_FOR_SERVICE`) are
        skipped silently.
      * ``managed_users`` only: PIN columns don't exist on
        ``server_users``.

    The encrypted ciphertext is reused per linked row rather than
    re-encrypted. Fernet would mint a fresh nonce per call but decrypt
    to the same plaintext, so the blob is irrelevant once we know it
    round-trips.

    No-op when the origin has no app_user_uuid (legacy row pre-v12) or
    when no linked rows exist. Defensive: per-row failures are caught
    + logged and the loop continues."""
    origin_uuid = get_managed_user_app_uuid(origin_server_id, origin_username)
    if not origin_uuid:
        return 0
    class_uuids = _equivalence_class_for_uuid(origin_uuid)
    if not class_uuids:
        return 0
    origin_service_norm = (origin_service_type or "").lower()
    conn = _require_conn()
    updated = 0
    with _DB_LOCK:
        for uuid in class_uuids:
            if uuid == origin_uuid:
                continue  # already updated by the caller
            row = get_row_by_app_user_uuid(uuid)
            if row is None:
                continue
            if row.get("table") != "managed_users":
                continue
            row_service = (row.get("service_type") or "").lower()
            target_column = _PIN_COLUMN_FOR_SERVICE.get(row_service)
            if target_column is None:
                continue  # backend has no PIN-equivalent column
            same_backend = row_service == origin_service_norm
            if not same_backend and not cross_backend:
                continue  # cross-backend propagation is opt-in
            try:
                cur = conn.execute(
                    f"UPDATE managed_users SET {target_column} = ?, "
                    "updated_at = ? WHERE app_user_uuid = ?",
                    (encrypted_value, now, uuid),
                )
                updated += cur.rowcount or 0
            except Exception:
                log.exception(
                    "PIN propagation: per-row UPDATE failed for uuid=%r "
                    "(server=%r, handle=%r); continuing.",
                    uuid, row.get("server_id"), row.get("handle"),
                )
    return updated


def set_managed_user_display_name(
    *,
    server_id: str,
    username: str,
    display_name: Optional[str],
    propagate_to_linked: bool = True,
) -> Dict[str, Any]:
    """
    Update the per-user friendly display name. ``None`` or empty
    string clears it (UI falls back to the raw username).

    Propagation: when ``propagate_to_linked`` is True (default), the
    new display name is also written to every other (server, user)
    row linked to this one via ``user_identity_map``. Walks the
    equivalence class via
    :func:`_equivalence_class_for_uuid`, finds each linked row via
    :func:`get_row_by_app_user_uuid`, and updates managed_users +
    server_users in the same transaction. The cross-row UPDATEs are
    best-effort: a per-row failure is logged and the rest of the
    propagation continues.

    Pass ``propagate_to_linked=False`` to override propagation when
    the caller explicitly wants the new display name to apply to
    THIS server only - e.g., the operator labels the same human
    differently on two servers on purpose. The default reflects the
    common case ("rename me everywhere when I update my friendly
    name in one place")."""
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    dn = (display_name or "").strip() or None
    conn = _require_conn()
    now = time.time()
    with _DB_LOCK:
        cur = conn.execute(
            "UPDATE managed_users SET display_name = ?, updated_at = ? "
            "WHERE server_id = ? AND username = ?",
            (dn, now, server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
        # Mirror to the server_users row on the same (server, user) so
        # both tables stay in lockstep. Best-effort: a missing
        # server_users row is fine (the table is populated on first
        # snapshot capture; managed_users rows can pre-date it).
        try:
            conn.execute(
                "UPDATE server_users SET display_name = ? "
                "WHERE server_id = ? AND user_handle = ?",
                (dn, server_id, username),
            )
        except Exception:
            log.exception(
                "set_managed_user_display_name: server_users mirror "
                "update failed for (%r, %r); continuing.",
                server_id, username,
            )
    # Propagate across identity_map links. Outside the writer lock so
    # the per-linked-row UPDATEs each re-acquire the lock on their
    # own short call (avoids holding the lock across BFS reads).
    if propagate_to_linked:
        try:
            from server import media_db as _md
            _md._propagate_display_name_across_links(
                origin_server_id=server_id,
                origin_username=username,
                display_name=dn,
                now=now,
            )
        except Exception:
            log.exception(
                "set_managed_user_display_name: identity-link propagation "
                "failed for (%r, %r); the origin row was updated but "
                "linked rows may be stale.",
                server_id, username,
            )
    out = get_managed_user(server_id, username)
    assert out is not None
    return out


def _propagate_display_name_across_links(
    *,
    origin_server_id: str,
    origin_username: str,
    display_name: Optional[str],
    now: float,
) -> int:
    """Walk the identity_map equivalence class for the origin row
    and write ``display_name`` onto every linked managed_users +
    server_users row. Returns the number of rows updated across
    both tables (not counting the origin row, which the caller
    already updated).

    Resolution: origin (server_id, username) -> app_user_uuid (via
    managed_users first, then server_users fallback) -> equivalence
    class of UUIDs -> per-UUID row lookup via
    :func:`get_row_by_app_user_uuid` -> per-row UPDATE on the matching
    table.

    No-op when the origin has no app_user_uuid (legacy row pre-v12)
    or when no linked rows exist. Defensive: per-row failures are
    caught + logged and the loop continues."""
    origin_uuid = (
        get_managed_user_app_uuid(origin_server_id, origin_username)
        or get_server_user_app_uuid(origin_server_id, origin_username)
    )
    if not origin_uuid:
        return 0
    class_uuids = _equivalence_class_for_uuid(origin_uuid)
    if not class_uuids:
        return 0
    conn = _require_conn()
    updated = 0
    with _DB_LOCK:
        for uuid in class_uuids:
            if uuid == origin_uuid:
                continue  # already updated by the caller
            row = get_row_by_app_user_uuid(uuid)
            if row is None:
                continue
            try:
                if row["table"] == "managed_users":
                    cur = conn.execute(
                        "UPDATE managed_users SET display_name = ?, updated_at = ? "
                        "WHERE app_user_uuid = ?",
                        (display_name, now, uuid),
                    )
                    updated += cur.rowcount or 0
                    # Mirror to server_users on the same (server, user)
                    # so both tables converge.
                    try:
                        cur2 = conn.execute(
                            "UPDATE server_users SET display_name = ? "
                            "WHERE server_id = ? AND user_handle = ?",
                            (display_name, row["server_id"], row["handle"]),
                        )
                        updated += cur2.rowcount or 0
                    except Exception:
                        pass
                else:  # server_users
                    cur = conn.execute(
                        "UPDATE server_users SET display_name = ? "
                        "WHERE app_user_uuid = ?",
                        (display_name, uuid),
                    )
                    updated += cur.rowcount or 0
                    # Mirror to managed_users if present.
                    try:
                        cur2 = conn.execute(
                            "UPDATE managed_users SET display_name = ?, updated_at = ? "
                            "WHERE server_id = ? AND username = ?",
                            (display_name, now, row["server_id"], row["handle"]),
                        )
                        updated += cur2.rowcount or 0
                    except Exception:
                        pass
            except Exception:
                log.exception(
                    "display-name propagation: per-row UPDATE failed for "
                    "uuid=%r (server=%r, handle=%r); continuing.",
                    uuid, row.get("server_id"), row.get("handle"),
                )
    return updated


def backfill_pin_across_links(app_user_uuid: str) -> int:
    """Backfill empty PIN columns from any populated PIN in the
    identity_map equivalence class anchored on ``app_user_uuid``.

    Additive only: never overwrites an existing PIN value. Cross-
    backend: a Plex Home PIN value can backfill an Emby row's
    ``emby_easy_pin_enc`` (same human, same PIN concept across
    backends). When multiple class members already store a PIN,
    picks the most-recently-updated one as the source.

    Gated by tunable ``auto_backfill_pin_from_identity_links``
    (default True). When the tunable is False, this helper is a
    no-op. Per-row failures are caught + logged and the loop
    continues.

    Returns the number of PIN columns actually populated by this
    call (zero on no-op or when nothing to backfill)."""
    try:
        from services import tunables as _tunables
        if not _tunables.auto_backfill_pin_from_identity_links():
            return 0
    except Exception:
        # Tunables module unavailable; default to enabled rather than
        # silently skipping the backfill.
        pass

    uuid = (app_user_uuid or "").strip()
    if not uuid:
        return 0
    class_uuids = _equivalence_class_for_uuid(uuid)
    if len(class_uuids) < 2:
        # No class or singleton: nothing to backfill from / to.
        return 0
    conn = _require_conn()
    # Snapshot each managed_users row in the class with its current
    # PIN column state. Each row's "natural" PIN column is chosen
    # by ``service_type`` so cross-backend backfill targets the right
    # column on the destination row.
    class_rows: List[Dict[str, Any]] = []
    for u in class_uuids:
        row = get_row_by_app_user_uuid(u)
        if row is None or row.get("table") != "managed_users":
            continue
        svc = (row.get("service_type") or "").lower()
        col = _PIN_COLUMN_FOR_SERVICE.get(svc)
        if col is None:
            continue  # backend has no PIN column (unknown backend)
        cur = conn.execute(
            f"SELECT {col} AS enc, updated_at FROM managed_users "
            "WHERE app_user_uuid = ?",
            (u,),
        ).fetchone()
        if cur is None:
            continue
        class_rows.append({
            "uuid":       u,
            "service":    svc,
            "column":     col,
            "enc":        cur["enc"],
            "updated_at": float(cur["updated_at"] or 0),
        })
    if not class_rows:
        return 0
    # Source = the most recently updated row in the class with a
    # populated PIN. The encrypted blob round-trips to the same
    # plaintext under Fernet regardless of which row's column it
    # lives in, so we copy the ciphertext directly (no re-encrypt).
    sources = [r for r in class_rows if r["enc"]]
    if not sources:
        return 0
    source = max(sources, key=lambda r: r["updated_at"])
    source_enc = source["enc"]
    now = time.time()
    updated = 0
    with _DB_LOCK:
        for r in class_rows:
            if r["enc"]:
                continue  # additive only
            try:
                cur = conn.execute(
                    f"UPDATE managed_users SET {r['column']} = ?, "
                    "updated_at = ? WHERE app_user_uuid = ?",
                    (source_enc, now, r["uuid"]),
                )
                updated += cur.rowcount or 0
            except Exception:
                log.exception(
                    "backfill_pin_across_links: per-row UPDATE failed "
                    "for uuid=%r (col=%r); continuing.",
                    r["uuid"], r["column"],
                )
    if updated:
        log.info(
            "backfill_pin_across_links: filled %d empty PIN column(s) "
            "from class anchored on %r (source uuid=%r, svc=%r).",
            updated, uuid, source["uuid"], source["service"],
        )
    return updated


def get_managed_user_credential(
    server_id: str,
    username: str,
    kind: str,
) -> Optional[str]:
    """
    Decrypt and return one credential cell, or ``None`` if not stored.
    Intended for engine code that actually needs to USE a stored
    credential (pre-flight check, future per-user impersonation). The
    User Management read API never calls this - the panel only ever
    surfaces presence booleans.
    """
    if kind not in _CRED_COLUMN:
        raise ValueError(f"Unknown credential kind {kind!r}.")
    from server.secrets import decrypt_str
    column = _CRED_COLUMN[kind]
    conn = _require_conn()
    row = conn.execute(
        f"SELECT {column} AS enc FROM managed_users "
        "WHERE server_id = ? AND username = ?",
        (server_id, username),
    ).fetchone()
    found = bool(row is not None and row["enc"])
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_read(
            table="managed_users",
            field=column,
            where={"server_id": server_id, "username": username, "found": found},
            intent=f"fetch encrypted {kind} for engine use",
        )
    except Exception:
        pass
    if not found:
        return None
    try:
        return decrypt_str(row["enc"]) or None
    except Exception:
        # Malformed ciphertext (key rotated, file corruption). Treat
        # as "not stored" so the caller falls back to the no-credential
        # path rather than crashing the request.
        log.exception(
            "decrypt failed for managed_user %r on server %r (kind=%s); "
            "falling back to no-credential.",
            username, server_id, kind,
        )
        return None


def delete_managed_user(server_id: str, username: str) -> None:
    """
    Remove a managed-user row entirely. Raises if no such row.

    M13: hard-deletes a row holding Fernet-encrypted credentials
    (auth token, Plex Home PIN, service password), so it emits a
    ``db_access_log`` entry - matching ``purge_server_data`` /
    ``prune_stale_items`` and every other destructive media.db op.
    """
    if not server_id or not username:
        raise ValueError("server_id and username are required")
    conn = _require_conn()
    with _DB_LOCK:
        cur = conn.execute(
            "DELETE FROM managed_users WHERE server_id = ? AND username = ?",
            (server_id, username),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"No managed user {username!r} on server {server_id!r}."
            )
    try:
        from services.run_logs import db_access as db_access_log
        db_access_log.log_write(
            table="managed_users",
            where={"server_id": server_id, "username": username},
            affected_rows=cur.rowcount,
            intent="delete managed user (encrypted credentials destroyed)",
        )
    except Exception:
        log.exception("db_access_log emit failed for delete_managed_user")
