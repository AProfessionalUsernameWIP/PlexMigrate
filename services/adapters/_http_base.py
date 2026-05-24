"""
Shared HTTP plumbing for the Jellyfin / Emby adapters (and any future
HTTP-based backend).

The Plex adapter wraps plexapi which has its own session lifecycle;
this module is not used there. Jellyfin and Emby both speak HTTP+JSON
directly, share the same ``MediaBrowser`` / ``Emby`` Authorization
header family (with one notable schema difference), and benefit from
the same retry adapter + telemetry hooks the rest of the engine uses.

What lives here
---------------

* :class:`AuthCredentials` - per-server header builder. Differs between
  Jellyfin and Emby on a single string ("MediaBrowser" vs "Emby") plus
  the placement of ``UserId``. One dataclass with a flag is cheaper
  than two near-duplicate classes.
* :func:`make_session` - constructs a ``requests.Session`` with the
  shared retry adapter from ``services/auth.py:_make_retry_adapter``,
  installs the existing telemetry hook, registers with the hot-reload
  weak set, and primes the auth headers. Drop-in replacement for
  ``requests.Session()`` in the new adapters.
* :class:`HttpMediaAdapterMixin` - small mixin offering the session
  helper + a paginated GET helper. Both adapters will subclass
  ``MediaServerAdapter`` and mix this in for the boilerplate.

These adapters use ``requests`` + the existing retry adapter rather
than adding ``httpx`` as a new dependency: one fewer external
dependency, and the telemetry / pool / retry plumbing stays uniform
across all adapters.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urljoin

import requests


log = logging.getLogger("plexmigrate.services.adapters._http_base")


# ── Auth header construction ────────────────────────────────────────────────
#
# Jellyfin and Emby both speak the "MediaBrowser-style" Authorization
# header. The scheme name and per-field shape differ:
#
#   Jellyfin (modern):
#       Authorization: MediaBrowser Token="<token>", Client="...", Device="...",
#                      DeviceId="...", Version="..."
#       (Token may also be passed via legacy X-Emby-Token header; new
#        builds prefer the Authorization form. See
#        https://api.jellyfin.org/ and the linked Gist
#        https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f
#        which is the canonical doc on the format.)
#
#   Emby:
#       Authorization: Emby UserId="<user-id>", Token="<token>", Client="...",
#                      Device="...", DeviceId="...", Version="..."
#       (Per dev.emby.media/doc/restapi/User-Authentication.html. After
#        authentication, subsequent requests may use X-Emby-Token in place
#        of the full header.)
#
# The differences are: scheme name, and whether UserId rides on the
# header. We expose one dataclass with a backend flag and emit the
# right shape from one place.

_CLIENT_NAME = "PlexMigrate"
_DEVICE_NAME = "PlexMigrate Engine"


@dataclass
class AuthCredentials:
    """Per-server auth shape for Jellyfin / Emby. ``token`` is the access
    token or API key. ``backend`` controls the header scheme name.
    ``user_id`` is required for Emby (rides in the header) and optional
    for Jellyfin (omitted from the header)."""
    backend: str                # "jellyfin" | "emby"
    token: str
    user_id: Optional[str] = None
    device_id: str = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    client: str = _CLIENT_NAME
    device: str = _DEVICE_NAME
    version: str = "1.0"

    def authorization_header(self, *, user_id_override: Optional[str] = None) -> str:
        """Build the ``Authorization`` header value for this credential.

        Pairs are emitted in the order Plex / Jellyfin / Emby clients
        conventionally use. Order isn't required by spec but matches
        what server-side parsers have been observed to log most
        predictably.

        ``user_id_override`` lets a single call swap the
        Authorization's ``UserId="..."`` to a target user without
        rebuilding the session. The fix scenario: an admin token
        impersonates a managed user via ``/Users/{target_uid}/Items``,
        but Emby contextualises ``UserData`` (PlayCount, Rating,
        IsFavorite) against the Authorization header's UserId rather
        than the URL path's. Result pre-fix: every per-user iter_items
        walk returned items with view_count=0 even when the target
        user had played plenty. Per-call override gives the request
        the target user's UserId in the header so Emby fills UserData
        correctly. The session's default UserId stays the admin's
        (for endpoints that genuinely need admin scope)."""
        scheme = "MediaBrowser" if self.backend == "jellyfin" else "Emby"
        parts = []
        effective_user_id = user_id_override if user_id_override is not None else self.user_id
        if self.backend == "emby" and effective_user_id:
            parts.append(f'UserId="{effective_user_id}"')
        # Omit the ``Token`` field entirely when we don't
        # have one yet (pre-authentication call to AuthenticateByName).
        # Some Emby builds reject ``Token=""`` outright at the
        # auth-header pre-flight check (before the actual credentials
        # are validated), surfacing as a 401 with no body. Sending the
        # field only when we have a real token matches the Emby /
        # Jellyfin community-recommended shape for the
        # MediaBrowser-style header and is back-compat for every
        # subsequent authenticated request.
        if self.token:
            parts.append(f'Token="{self.token}"')
        parts.extend([
            f'Client="{self.client}"',
            f'Device="{self.device}"',
            f'DeviceId="{self.device_id}"',
            f'Version="{self.version}"',
        ])
        return f"{scheme} " + ", ".join(parts)

    def headers(self) -> Dict[str, str]:
        """Default headers for every request: Authorization + Accept JSON."""
        return {
            "Authorization": self.authorization_header(),
            "Accept": "application/json",
        }


# ── Session factory ─────────────────────────────────────────────────────────
#
# Reuses the existing retry adapter + telemetry hook + dashboard plumbing
# from services/auth.py so Jellyfin / Emby HTTP traffic shows up on the
# same Network panel + activity feed Plex traffic does. Lazy import so
# this module is loadable in CLI-only checkouts that don't pull in the
# full services package.

def make_session(creds: AuthCredentials) -> requests.Session:
    """Build a ``requests.Session`` configured for this backend.

    The session has:

    * Retry adapter mounted on both ``http://`` and ``https://`` via
      ``services.auth._make_retry_adapter`` so 429 / 5xx are retried
      uniformly across all backends.
    * The shared response telemetry hook installed so per-call latency
      + status histograms populate the dashboard.
    * Registered with the hot-reload weak set so a future tunables
      change rebuilds the adapter on this session too.
    * Default ``Authorization`` + ``Accept`` headers primed.
    """
    from services.auth import (  # local import: see module docstring
        _install_response_hook,
        _make_retry_adapter,
        _register_session,
    )

    session = requests.Session()
    adapter = _make_retry_adapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    _install_response_hook(session)
    _register_session(session)
    session.headers.update(creds.headers())
    return session


# ── Mixin offering paginated GET + url join ─────────────────────────────────
#
# The Jellyfin / Emby item-list endpoint (``GET /Users/{id}/Items``) is
# paginated via ``StartIndex`` + ``Limit``. Adapters that walk a whole
# library make repeated calls; centralising the pagination loop here
# keeps the per-adapter code focused on the response shape.

class HttpMediaAdapterMixin:
    """Shared HTTP helpers for Jellyfin / Emby adapters. Subclasses set
    ``self._base_url`` and ``self._session`` in ``__init__`` then
    consume :meth:`_get_json` / :meth:`_paginate`."""

    _base_url: str
    _session: requests.Session
    _default_page_size: int = 200

    def _url(self, path: str) -> str:
        """Resolve ``path`` against the server base URL. ``path`` may be
        a leading-slash absolute path (the common case) or a fully
        formed URL (passes through)."""
        if path.startswith(("http://", "https://")):
            return path
        # urljoin needs the base to end with a slash for path-relative
        # joins to work correctly.
        base = self._base_url if self._base_url.endswith("/") else self._base_url + "/"
        return urljoin(base, path.lstrip("/"))

    def _get_json(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
        extra_headers: Optional[Dict[str, Optional[str]]] = None,
    ) -> Any:
        """GET ``path`` and return the parsed JSON body. Raises
        ``requests.HTTPError`` on non-2xx. Caller handles fail-soft
        semantics where applicable.

        ``extra_headers`` merges into the session headers for this
        single request (per-call wins on collisions). A value of
        ``None`` STRIPS that header from the final request even if
        it's set in the session's default headers; per requests'
        ``merge_setting`` behavior in ``sessions.py``, None values
        get filtered out after the merge. Used by per-user
        iter_items to strip the admin Authorization header while
        keeping X-Emby-Token scoped to the target user."""
        resp = self._session.get(
            self._url(path), params=params or {}, timeout=timeout,
            headers=extra_headers or None,
        )
        resp.raise_for_status()
        return resp.json()

    def _post_json(
        self,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        """POST with a JSON body and return the parsed JSON response (or
        ``None`` for 204)."""
        resp = self._session.post(
            self._url(path),
            json=json_body,
            params=params or {},
            timeout=timeout,
        )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _delete(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> None:
        """DELETE ``path``. Raises on non-2xx."""
        resp = self._session.delete(
            self._url(path), params=params or {}, timeout=timeout,
        )
        resp.raise_for_status()

    def _paginate(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        page_size: Optional[int] = None,
        items_key: str = "Items",
        total_key: str = "TotalRecordCount",
        extra_headers: Optional[Dict[str, Optional[str]]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Walk a paginated Jellyfin / Emby list endpoint and yield each
        item dict. Stops when ``StartIndex + len(page) >= TotalRecordCount``
        or when an empty page comes back.

        ``extra_headers`` is forwarded to every paginated request so a
        per-user iter_items walk can keep the same target-user
        Authorization across page boundaries."""
        params = dict(params or {})
        size = page_size or self._default_page_size
        start = 0
        while True:
            params["StartIndex"] = start
            params["Limit"] = size
            payload = self._get_json(path, params=params, extra_headers=extra_headers)
            if not isinstance(payload, dict):
                # Some endpoints return a bare list when there's no
                # pagination envelope; surface those directly.
                if isinstance(payload, list):
                    for item in payload:
                        yield item
                return
            page = payload.get(items_key) or []
            if not page:
                return
            for item in page:
                yield item
            total = payload.get(total_key)
            if total is not None and start + len(page) >= int(total):
                return
            if len(page) < size:
                return
            start += len(page)


__all__ = [
    "AuthCredentials",
    "make_session",
    "HttpMediaAdapterMixin",
]
