"""Plex per-user watch / rating / progress write mixin.

All three writes go through the direct HTTP path
(``services.state._session`` + ``/:/scrobble`` etc.) rather than
plexapi's higher-level methods. This matches what the existing
restorer does and inherits the same token-in-header behaviour
(M1: never put the token in the query string).

``set_favorite`` stays at the :class:`MediaServerAdapter` default
(unsupported) — Plex has no per-user favorite toggle, ratings are
the only per-user signal.

Mixed into ``PlexAdapter``. The helpers ``get_current_view_count``
and ``get_view_state`` are read-side companions to ``set_watched``
and are colocated here because the only callers are the write-side
restorers (Replace mode delta computation + JOBS-03 latest_wins
conflict policy)."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import services.state as state

from . import ItemRef, UserContext, WriteResult


log = logging.getLogger("plexmigrate.services.adapters.plex")


class PlexWritesMixin:
    """Per-user write methods for :class:`PlexAdapter`.

    Owns the ``/:/scrobble`` / ``/:/progress`` / ``/:/rate`` HTTP
    surface and the read-side helpers (``get_current_view_count``,
    ``get_view_state``) used to drive exact-target writes.

    Depends on ``self._server_for`` (per-user PlexServer factory)
    + ``self._base_url`` + ``self._admin_token`` being defined on
    the concrete adapter."""

    def get_current_view_count(
        self,
        item_ref: ItemRef,
        *,
        user_context: UserContext,
    ) -> Optional[int]:
        """Read this item's CURRENT ``viewCount`` for the user via
        plexapi's ``server.fetchItem``. Used by the adapter restorer's
        Replace mode to compute the exact-target delta before calling
        ``set_watched``. Falls back to None on any failure so the
        restorer can keep going under delta-mode semantics."""
        try:
            server = self._server_for(user_context)
            it = server.fetchItem(item_ref.backend_item_id)
            return int(getattr(it, "viewCount", 0) or 0)
        except Exception:
            return None

    def get_view_state(
        self,
        item_ref: ItemRef,
        *,
        user_context: UserContext,
    ) -> Optional[Tuple[int, Optional[float]]]:
        """Read this item's ``viewCount`` and ``lastViewedAt`` for the
        user in one ``fetchItem`` call. Powers the sync engine's
        ``latest_wins`` conflict policy (JOBS-03). None on any failure;
        the timestamp is None when the item was never viewed."""
        try:
            server = self._server_for(user_context)
            it = server.fetchItem(item_ref.backend_item_id)
            count = int(getattr(it, "viewCount", 0) or 0)
            last_viewed = getattr(it, "lastViewedAt", None)
            ts: Optional[float] = None
            if last_viewed is not None:
                try:
                    ts = last_viewed.timestamp()
                except Exception:
                    ts = None
            return (count, ts)
        except Exception:
            return None

    def set_watched(
        self,
        item_ref: ItemRef,
        *,
        view_count: int,
        last_viewed_at: Optional[float],
        user_context: UserContext,
        current_view_count: Optional[int] = None,
    ) -> WriteResult:
        """Plex has no "set view count to N" endpoint; the only
        primitives are ``/:/scrobble`` (+1) and ``/:/unscrobble``
        (reset to 0). The adapter implements exact-target writes by
        choosing the right combination:

          * ``current_view_count`` provided:
            - target == 0          -> 1 unscrobble
            - target == current    -> noop
            - target > current     -> (target - current) scrobbles
            - 0 < target < current -> 1 unscrobble + target scrobbles
          * ``current_view_count`` is None (legacy callers, e.g.
            ``services.restore.plex_native`` which precomputes the delta):
            - target == 0          -> noop (caller already handled it
              via plexapi's ``markUnplayed``)
            - target > 0           -> ``view_count`` scrobbles (the
              caller's pre-computed delta)

        ``state.VIEWCOUNT_INCREMENT_CAP`` clamps the total scrobble
        count per call so a stuck loop can't fire thousands of calls
        on a bad input."""
        cap = int(getattr(state, "VIEWCOUNT_INCREMENT_CAP", 999))
        target = max(0, int(view_count))
        token = user_context.auth_token or self._admin_token
        scrobble_url = f"{self._base_url}/:/scrobble"
        unscrobble_url = f"{self._base_url}/:/unscrobble"
        params = {
            "key": item_ref.backend_item_id,
            "identifier": "com.plexapp.plugins.library",
        }
        headers = {"X-Plex-Token": token}

        def _scrobble_n(n: int) -> None:
            for _ in range(max(0, min(int(n), cap))):
                resp = state._session.get(
                    scrobble_url, params=params,
                    headers=headers, timeout=5,
                )
                # A 4xx/5xx means the scrobble did not land; raise so
                # the enclosing try returns WriteResult.fail, not ok.
                resp.raise_for_status()

        def _unscrobble_once() -> None:
            resp = state._session.get(
                unscrobble_url, params=params,
                headers=headers, timeout=5,
            )
            resp.raise_for_status()

        try:
            if current_view_count is None:
                # Legacy delta-mode: caller has already done the math.
                if target == 0:
                    return WriteResult.ok(
                        "no increments needed (target count 0)"
                    )
                _scrobble_n(target)
                return WriteResult.ok(f"incremented {target} time(s)")
            # Exact-target mode.
            current = max(0, int(current_view_count))
            if target == current:
                return WriteResult.ok(
                    f"already at target ({target}); noop"
                )
            if target == 0:
                _unscrobble_once()
                return WriteResult.ok("unscrobbled to 0")
            if target > current:
                delta = target - current
                _scrobble_n(delta)
                return WriteResult.ok(
                    f"scrobbled +{delta} to reach {target} "
                    f"(was {current})"
                )
            # 0 < target < current: reset to 0 then scrobble up.
            _unscrobble_once()
            _scrobble_n(target)
            return WriteResult.ok(
                f"unscrobble + {target} scrobble(s) to reach {target} "
                f"(was {current})"
            )
        except Exception as exc:
            return WriteResult.fail(f"scrobble failed: {exc}")

    def set_resume_position(
        self,
        item_ref: ItemRef,
        offset_ms: int,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        token = user_context.auth_token or self._admin_token
        url = f"{self._base_url}/:/progress"
        try:
            resp = state._session.get(
                url,
                params={
                    "key": item_ref.backend_item_id,
                    "identifier": "com.plexapp.plugins.library",
                    "time": int(offset_ms),
                    "state": "stopped",
                    "hasMDE": 1,
                },
                headers={"X-Plex-Token": token},
                timeout=5,
            )
            resp.raise_for_status()
        except Exception as exc:
            return WriteResult.fail(f"progress failed: {exc}")
        return WriteResult.ok()

    def set_rating(
        self,
        item_ref: ItemRef,
        rating: float,
        *,
        user_context: UserContext,
    ) -> WriteResult:
        token = user_context.auth_token or self._admin_token
        url = f"{self._base_url}/:/rate"
        try:
            resp = state._session.put(
                url,
                params={
                    "key": item_ref.backend_item_id,
                    "identifier": "com.plexapp.plugins.library",
                    "rating": float(rating),
                },
                headers={"X-Plex-Token": token},
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as exc:
            return WriteResult.fail(f"rate failed: {exc}")
        return WriteResult.ok()

    # set_favorite stays at the ABC default (unsupported).
