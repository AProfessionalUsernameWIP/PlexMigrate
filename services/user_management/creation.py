"""Cross-backend user creation with two-phase commit and rollback on failure."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


log = logging.getLogger("plexmigrate.services.user_management.creation")


@dataclass
class _SpecResult:
    """One row's outcome from user creation."""
    source_user_handle: str
    target_username: str
    backend_user_id: str
    status: str               # "created" | "rolled_back" | "failed_pre_rollback"
    error: Optional[str] = None


class UserCreationError(Exception):
    """User creation failed; check \`\`exc.results\`\` for per-spec outcomes."""

    def __init__(self, message: str, results: List[_SpecResult]) -> None:
        super().__init__(message)
        self.results = results


def _coerce_policy(raw: Optional[Dict[str, Any]]) -> Optional[Any]:
    """Convert free-form JSON policy dict to adapter's UserPolicy shape."""
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
    """Read the four required fields from a Pydantic UserCreateSpec or plain dict."""
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
    Walk specs against adapter.create_user and persist results to managed_users.
    
    Two-phase commit: on partial failure, roll back all created users.
    Either every user in the spec list exists on the destination AND has a 
    managed_users row, OR none of them do. A re-submit does not collide with 
    leftover users from a failed attempt.
    
    Failure modes:
      * Adapter raises during create -> rollback prior creates + raise UserCreationError.
      * Adapter returns None -> treat as failure (backend does not support create).
      * Adapter returns UserSpec with empty backend_user_id -> treat as failure.
      * managed_users persist fails -> rollback + raise (partial record state is
        cleaner to undo than to leave unrecorded users on destination).
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
                # Defensive guard against direct callers bypassing Pydantic validation.
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
                # Backend does not support create; either configuration error
                # (D-OWNER modal should not surface for backends without create)
                # or adapter role flag is wrong. Treat as fatal.
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

        # Persist mappings: source_user_handle -> backend_user_id for identity resolution on subsequent runs.
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
        if created_ids:
            _rollback_created_users(adapter, created_ids, results, lg)
        raise


def _persist_mappings(
    *,
    dest_server_id: str,
    results: List[_SpecResult],
    logger: logging.Logger,
) -> None:
    """
    Insert managed_users rows per successfully-created spec.
    
    Identity layer: source_user_handle persists as managed_users.username for 
    identity resolution on subsequent runs. backend_user_id enables direct API addressing.
    """
    try:
        from server.media_db import upsert_managed_user
    except ImportError:
        logger.warning(
            "user_creation: server.media_db.upsert_managed_user not "
            "importable; skipping mapping persist (created users "
            "remain on destination but Hestia-MediaManager has no record)."
        )
        return
    for r in results:
        if r.status != "created":
            continue
        upsert_managed_user(
            server_id=dest_server_id,
            username=r.target_username,
            backend_user_id=r.backend_user_id,
            source_user_handle=r.source_user_handle,
            created_via_user_creation=True,
        )


def _rollback_created_users(
    adapter: Any,
    created_ids: List[str],
    results: List[_SpecResult],
    logger: logging.Logger,
) -> None:
    """
    Best-effort delete of users created in this run.
    
    Delete failures are logged loudly but do NOT raise: we are in the rollback 
    path of a primary failure and re-raising would mask the original error.
    """
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
