"""
EmbyAdapter: Emby Server implementation of
:class:`services.adapters.MediaServerAdapter`.

Emby and Jellyfin share a common ancestor (Emby's pre-3.6 codebase
that Jellyfin forked from) and consequently share most of the REST
API surface. The differences relevant to Hestia-MediaManager are:

1. **Authorization scheme name.** Emby uses
   ``Authorization: Emby UserId=..., Token=..., Client=..., ...``;
   Jellyfin uses ``Authorization: MediaBrowser Token=..., Client=...,
   ...`` and does NOT carry ``UserId`` in the header. Handled inside
   :class:`services.adapters._http_base.AuthCredentials` by flipping
   the ``backend`` flag.
2. **Per-user-token vs admin-token writes.** Both backends accept
   admin-token-with-UserId-in-URL writes, so the engine's pattern
   ("admin token + UserContext carries target user id") works for
   both.
3. **Rating semantics.** Numeric ``UserData.Rating`` and binary
   ``IsFavorite`` are exposed the same way on both backends.

Capability-wise EmbyAdapter mirrors JellyfinAdapter. Subclassing
JellyfinAdapter keeps the per-method code in one place; the constructor
flips ``_auth_scheme`` and the ``backend`` class attribute, and the
constructor rebuilds the session with Emby-flavored credentials.

Reference docs used:
- https://dev.emby.media/doc/restapi/User-Authentication.html
- https://dev.emby.media/reference/RestAPI.html
- https://dev.emby.media/reference/RestAPI/PlaystateService.html
- https://emby.media/support/articles/Webhooks.html
"""

from __future__ import annotations

import logging
from typing import Optional

from ._http_base import AuthCredentials, make_session
from .jellyfin import JellyfinAdapter


log = logging.getLogger("plexmigrate.services.adapters.emby")


class EmbyAdapter(JellyfinAdapter):
    """Emby implementation. Differs from JellyfinAdapter only in the
    Authorization header scheme + the inclusion of ``UserId`` on the
    header for authenticated requests.

    Per the roadmap's "30 percent override threshold" guideline,
    subclassing remains acceptable because the divergence is currently
    only the header builder + identity probe path. If Emby and
    Jellyfin diverge meaningfully in PR-CrossPolish (cross-backend
    writes), the right refactor is to promote a shared
    ``_HttpMediaAdapter`` sibling base class and demote both Emby and
    Jellyfin to peer subclasses; do not let the override chain
    grow."""

    backend: str = "emby"
    _auth_scheme: str = "emby"

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        *,
        owner_user_id: Optional[str] = None,
        machine_id: Optional[str] = None,
        server_uid: Optional[str] = None,
    ) -> None:
        super().__init__(
            base_url,
            admin_token,
            owner_user_id=owner_user_id,
            machine_id=machine_id,
            server_uid=server_uid,
        )
        # Emby diverges from the Jellyfin base in ways the parent
        # __init__ does not cover: Emby carries ``UserId`` in the
        # Authorization header (Jellyfin does not), so rebuild the
        # credentials + session with the owner id. Emby also accepts a
        # UserId-free ``X-Emby-Token`` header, set here so
        # construction-time calls work before server_identity()
        # resolves the owner. See
        # https://dev.emby.media/doc/restapi/User-Authentication.html
        self._creds = AuthCredentials(
            backend=self._auth_scheme,
            token=admin_token,
            user_id=self._owner_user_id or None,
        )
        self._session = make_session(self._creds)
        self._session.headers["X-Emby-Token"] = admin_token

    def server_identity(self):
        """Emby override: after resolving owner_user_id, rebuild the
        Authorization header so subsequent endpoints that DO require
        ``UserId`` in the header (per-user write paths) carry it.

        Without this, the constructor's auth header omits UserId and
        Emby's stricter endpoints would 401 on the first per-user
        write. Idempotent: cached identity short-circuits."""
        if self._identity_cache is not None:
            return self._identity_cache
        # Parent class resolves the identity (via /Users/Me, falling
        # back to /Users for admin discovery). Side-effect: sets
        # self._owner_user_id.
        identity = super().server_identity()
        # Refresh the auth header now that we know the user id.
        if self._owner_user_id and self._creds.user_id != self._owner_user_id:
            self._creds = AuthCredentials(
                backend=self._auth_scheme,
                token=self._admin_token,
                user_id=self._owner_user_id,
            )
            self._session.headers["Authorization"] = self._creds.authorization_header()
        return identity

    def invalidate_identity_cache(self) -> None:
        """Force the next ``server_identity()`` call to re-probe the
        backend instead of returning the cached value.

        Identity is server-scoped and effectively immutable for a
        registered Emby server's lifetime (the owner_user_id of a
        backend doesn't change in normal operation). The two cases
        that justify invalidation:

        * The operator transfers Emby server ownership to a different
          admin account. The cached ``owner_user_id`` is now stale.
        * The Emby admin password / API key was rotated, and the
          adapter needs to re-probe to pick up the new identity if
          /Users/Me responses change for any reason.

        Both are operator-driven events, not engine-driven, so the
        invalidation is exposed as a public method rather than tied
        to an automatic invalidation rule. Callers: server registry's
        edit-server endpoint (after a token/key change) and any
        future "transfer ownership" surface.
        """
        self._identity_cache = None

    # Everything else is inherited from JellyfinAdapter.
    #
    # If Emby's response shapes diverge from Jellyfin's in future
    # versions (e.g. UserData fields, BoxSet endpoints, etc.) the
    # right move is to override the affected method here rather than
    # widening JellyfinAdapter's behaviour conditionally.


__all__ = ["EmbyAdapter"]
