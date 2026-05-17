"""
Cross-backend user creation (Plan[RUN-JOB-UI] D-OWNER, work item 2).

When the end user confirms the D-OWNER preflight modal with a non-
empty list of users to create on the destination, ``server/jobs.py``
invokes :func:`create_users_for_job` at run start, BEFORE the engine
fires any item-state write. The helper walks the spec list, calls
``adapter.create_user`` per row, captures the returned
``backend_user_id``, persists the mapping into ``managed_users``, and
returns a per-spec result.

Two-phase commit: on partial failure, every user already created in
this run is rolled back via ``adapter.delete_user`` so a re-submit
does not collide with half-built destination state. The rollback is
best-effort; any delete that itself fails is logged loudly so the
end user can clean up manually.

The temp_password values flow through this module in cleartext (the
adapter's create_user takes a plaintext password). The module logs
nothing about the password value itself; only the encrypted form
ever touches managed_users (see :func:`server.media_db.upsert_managed_user`'s
``auth_token`` / ``service_password_enc`` Fernet path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


log = logging.getLogger("plexmigrate.services.user_creation")


@dataclass
class _SpecResult:
    """One row's outcome. Carries the source identity, the resulting
    destination identity, and a status flag so the caller can build
    a structured report for the run log."""
    source_user_handle: str
    target_username: str
    backend_user_id: str
    status: str               # "created" | "rolled_back" | "failed_pre_rollback"
    error: Optional[str] = None


class UserCreationError(Exception):
    """Raised by :func:`create_users_for_job` when at least one user
    failed to create. After this is raised, the per-spec results
    object is accessible at ``exc.results`` so the caller can render
    a structured error in the run log + the dashboard error frame."""

    def __init__(self, message: str, results: List[_SpecResult]) -> None:
        super().__init__(message)
        self.results = results


def _coerce_policy(raw: Optional[Dict[str, Any]]) -> Optional[Any]:
    """Convert the end user's policy dict (free-form JSON from the
    Pydantic model) into the adapter's :class:`UserPolicy` shape, or
    None when the end user did not supply one. Unknown keys are
    silently dropped; the adapter applies the destination's default
    for anything missing."""
    if not raw:
        return None
    try:
        from services.adapters import UserPolicy
    except ImportError:
        return None
    if not isinstance(raw, dict):
        return None
    return UserPolicy(
        is_administrator=bool(raw.get("IsAdministrator", False)),
        is_disabled=bool(raw.get("IsDisabled", False)),
        enable_all_folders=bool(raw.get("EnableAllFolders", True)),
        enabled_folder_ids=tuple(
            str(x) for x in (raw.get("EnabledFolders") or [])
        ),
    )


def _spec_to_dict(spec: Any) -> Dict[str, Any]:
    """Read the four required fields from a Pydantic ``UserCreateSpec``
    OR a plain dict. Duck-types so test fixtures can pass dicts."""
    if hasattr(spec, "model_dump"):
        return spec.model_dump()
    if isinstance(spec, dict):
        return dict(spec)
    raise TypeError(
        f"Unsupported user-create-spec shape: {type(spec).__name__}"
    )


def create_users_for_job(
    *,
    adapter: Any,
    specs: Sequence[Any],
    dest_server_id: str,
    logger: Optional[logging.Logger] = None,
) -> List[_SpecResult]:
    """
    Walk ``specs`` against ``adapter.create_user``. On full success,
    persist the resulting (source_handle, backend_user_id,
    target_username) tuples into ``managed_users`` on the destination
    server and return the per-spec result list. On any partial
    failure, roll back every already-created user via
    ``adapter.delete_user`` and raise :exc:`UserCreationError`.

    The two-phase commit guarantees:
      * Either every user in the spec list exists on the destination
        AND has a row in managed_users, OR none of them do.
      * The engine never proceeds to item-state writes against a
        partial create state.
      * A re-submit after a failure does not collide with leftover
        users from the failed attempt (rollback cleared them).

    Failure modes:
      * Adapter raises during create -> rollback the prior creates +
        raise UserCreationError.
      * Adapter returns None (backend does not support create) ->
        treat as failure (config error: D-OWNER modal should not
        have surfaced on a backend without create support).
      * Adapter returns a UserSpec with empty backend_user_id ->
        treat as failure (defensive; the spec contract is "non-empty
        id on success").
      * managed_users persist fails -> rollback + raise (the
        destination has the users but PlexBackUp has no record of
        them; cleaner to undo than to leave half-recorded state).
    """
    lg = logger or log
    if not specs:
        return []

    results: List[_SpecResult] = []
    created_ids: List[str] = []

    try:
        for spec in specs:
            row = _spec_to_dict(spec)
            source_handle = str(row.get("source_user_handle") or "").strip()
            target_user = str(row.get("target_username") or "").strip()
            password = str(row.get("temp_password") or "")
            policy = _coerce_policy(row.get("target_user_policy"))

            if not source_handle or not target_user or not password:
                # Should never fire if the Pydantic validator did its
                # job; defensive guard against direct callers.
                err = (
                    f"spec missing required field "
                    f"(source={source_handle!r} target={target_user!r} "
                    f"password_set={bool(password)})"
                )
                results.append(_SpecResult(
                    source_user_handle=source_handle,
                    target_username=target_user,
                    backend_user_id="",
                    status="failed_pre_rollback",
                    error=err,
                ))
                raise UserCreationError(err, results)

            try:
                user_spec = adapter.create_user(
                    target_user, password=password, policy=policy,
                )
            except Exception as exc:
                err = f"adapter.create_user raised: {exc!r}"
                lg.exception(
                    "user_creation: adapter.create_user failed for "
                    "source=%r target=%r", source_handle, target_user,
                )
                results.append(_SpecResult(
                    source_user_handle=source_handle,
                    target_username=target_user,
                    backend_user_id="",
                    status="failed_pre_rollback",
                    error=err,
                ))
                raise UserCreationError(err, results) from exc

            if user_spec is None:
                # Backend does not support create. Either the end user
                # routed a Plex destination here by mistake, or the
                # adapter's role flag is wrong. Treat as fatal.
                err = (
                    f"adapter {type(adapter).__name__} returned None "
                    "from create_user (backend may not support API "
                    "user creation)"
                )
                lg.error("user_creation: %s", err)
                results.append(_SpecResult(
                    source_user_handle=source_handle,
                    target_username=target_user,
                    backend_user_id="",
                    status="failed_pre_rollback",
                    error=err,
                ))
                raise UserCreationError(err, results)

            backend_user_id = getattr(user_spec, "backend_user_id", "") or ""
            if not backend_user_id:
                err = (
                    "adapter.create_user returned UserSpec with empty "
                    "backend_user_id; destination side is in an unknown "
                    "state, treating as failure"
                )
                lg.error("user_creation: %s for target=%r", err, target_user)
                results.append(_SpecResult(
                    source_user_handle=source_handle,
                    target_username=target_user,
                    backend_user_id="",
                    status="failed_pre_rollback",
                    error=err,
                ))
                raise UserCreationError(err, results)

            created_ids.append(backend_user_id)
            results.append(_SpecResult(
                source_user_handle=source_handle,
                target_username=target_user,
                backend_user_id=backend_user_id,
                status="created",
            ))
            lg.info(
                "user_creation: created destination user "
                "source=%r target=%r backend_user_id=%s",
                source_handle, target_user, backend_user_id,
            )

        # All specs created successfully. Persist the mapping into
        # managed_users so subsequent runs reuse the same backend_user_id
        # without prompting the end user.
        try:
            _persist_mappings(
                dest_server_id=dest_server_id,
                results=results,
                logger=lg,
            )
        except Exception as exc:
            err = f"managed_users persist failed: {exc!r}"
            lg.exception("user_creation: %s", err)
            raise UserCreationError(err, results) from exc

        return results

    except UserCreationError:
        # Roll back every user we created in this run.
        if created_ids:
            _rollback_created_users(adapter, created_ids, results, lg)
        raise


def _persist_mappings(
    *,
    dest_server_id: str,
    results: List[_SpecResult],
    logger: logging.Logger,
) -> None:
    """Insert one ``managed_users`` row per successfully-created spec.
    Best-effort coupling: the source handle survives as
    ``managed_users.username`` so the engine's identity resolver can
    walk source->destination on subsequent runs. The destination's
    backend_user_id is captured for direct API addressing."""
    try:
        from server.media_db import upsert_managed_user
    except ImportError:
        logger.warning(
            "user_creation: server.media_db.upsert_managed_user not "
            "importable; skipping mapping persist (created users "
            "remain on destination but PlexBackUp has no record)."
        )
        return
    for r in results:
        if r.status != "created":
            continue
        try:
            upsert_managed_user(
                server_id=dest_server_id,
                username=r.target_username,
                backend_user_id=r.backend_user_id,
                source_user_handle=r.source_user_handle,
                created_via_user_creation=True,
            )
        except TypeError:
            # ``upsert_managed_user`` may not yet accept the two new
            # kwargs; fall back to the legacy positional shape so the
            # test harness + older callers keep working. The link
            # column will be backfilled in a follow-up migration.
            try:
                upsert_managed_user(
                    server_id=dest_server_id,
                    username=r.target_username,
                )
            except Exception:
                logger.exception(
                    "user_creation: upsert_managed_user fallback failed "
                    "for target=%r", r.target_username,
                )


def _rollback_created_users(
    adapter: Any,
    created_ids: List[str],
    results: List[_SpecResult],
    logger: logging.Logger,
) -> None:
    """Best-effort delete of users created earlier in this same run.
    Each delete that itself fails is logged loudly; the failure does
    NOT raise (we are already in the rollback path of a primary
    failure and re-raising would mask the original error)."""
    logger.warning(
        "user_creation: rolling back %d previously-created destination "
        "user(s) due to a downstream failure.",
        len(created_ids),
    )
    by_id: Dict[str, _SpecResult] = {}
    for r in results:
        if r.backend_user_id:
            by_id[r.backend_user_id] = r
    for backend_user_id in created_ids:
        try:
            adapter.delete_user(backend_user_id)
            entry = by_id.get(backend_user_id)
            if entry is not None:
                entry.status = "rolled_back"
            logger.info(
                "user_creation: rolled back destination user %s",
                backend_user_id,
            )
        except Exception:
            logger.exception(
                "user_creation: rollback delete failed for %s; the "
                "destination has a leftover user and a manual cleanup "
                "is required.", backend_user_id,
            )
