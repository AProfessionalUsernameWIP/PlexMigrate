"""Request-scoped caller identity for audit-log attribution.

A FastAPI middleware decodes the JWT once per request and stashes the
authenticated username (the JWT ``sub``) into a ContextVar. Any code
path that emits an audit-log line can read it via :func:`current_caller`
without threading the request object through layers it has no other
reason to know about.

Best-effort: an unauthenticated request, a malformed token, or any
decode failure leaves the ContextVar at its default ``None`` value.
The middleware does not raise; it never blocks a request. Route
handlers are still responsible for enforcing role gates.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

log = logging.getLogger("plexmigrate.server.request_context")


_caller_username: ContextVar[Optional[str]] = ContextVar(
    "plexmigrate_caller_username", default=None,
)


def current_caller() -> Optional[str]:
    """Return the authenticated username of the current request, or
    ``None`` if the request is unauthenticated or the JWT could not
    be parsed."""
    return _caller_username.get()


def _decode_username(token: str) -> Optional[str]:
    """Decode the JWT and return its ``sub`` claim. Best-effort: any
    error (missing module, malformed token, expired signature) returns
    None. We import inside the function to avoid a hard dependency on
    the auth module at middleware-import time."""
    if not token:
        return None
    try:
        from server.auth_router import decode_token
    except Exception:
        return None
    try:
        payload = decode_token(token)
    except Exception:
        return None
    sub = payload.get("sub") if isinstance(payload, dict) else None
    return str(sub) if sub else None


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Populate the caller ContextVar for the duration of one request.

    Reads the bearer token from ``Authorization``. Falls back to
    inspecting the access-token cookie if present (some surfaces use
    cookie-only auth). Always restores the prior ContextVar value on
    the way out so concurrent requests cannot leak state between
    each other.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        username = _decode_username(token) if token else None
        reset_token = _caller_username.set(username)
        try:
            return await call_next(request)
        finally:
            _caller_username.reset(reset_token)
