// HTTP/fetch core and auth-token plumbing extracted from the legacy
// ``frontend/src/api.ts`` during Phase 2f. The per-resource REST
// helpers still in api.ts call ``http`` from this module; api.ts
// re-exports the public token API so all existing imports keep
// resolving unchanged.
//
// What lives here:
//   * Module-level mutable holders for the access token + the two
//     callback handlers (unauthorized / elevation-required).
//   * Public token API: setAccessToken / getAccessToken /
//     decodeJwtPayload / onUnauthorized / onElevationRequired.
//   * The ELEVATION_REQUIRED_DETAIL_MARKER constant the modal + backend
//     share.
//   * The silent-refresh helper ``refreshOnce`` and the
//     401/403-aware fetch wrapper ``http``.
//   * Internal accessors (``getRefreshOnce``, ``triggerUnauthorized``)
//     so the WebSocket clients still in ``api.ts`` can drive the same
//     refresh / logout flow they did when everything sat in one file.

import { PlaylistMgmtStructuredError } from './types';

// ── Auth ─────────────────────────────────────────────────────────────────────
//
// The opt-in JWT auth layer is gated by ``PLEXMIGRATE_AUTH_ENABLED``
// on the backend. The frontend treats it as cosmetic when disabled:
// the App loader hits ``/api/auth/status`` once on mount and either
// jumps straight to the main tabs (auth_enabled === false) or routes
// through setup / login.

// Module-level mutable holder for the access token. App.tsx sets and
// clears this via the setters below; every ``http()`` call below
// reads it just before issuing the request so a logout takes effect
// immediately. Held in a closure rather than React state because
// non-component code (the WebSocket helper, the http() wrapper) needs
// it without round-tripping through React's render cycle - and so
// that ``Main``'s mount-time effects see the post-login token
// synchronously, with no useEffect race in between.
let _accessToken: string | null = null;

// Callback fired when an API call returns 401 - App.tsx subscribes to
// this so it can drop into the login branch from anywhere a 401
// surfaces (which is anywhere, because every panel may call the API).
let _onUnauthorized: (() => void) | null = null;

// Callback fired when an API
// call returns 403 with the elevation-required detail. App.tsx
// registers a handler that opens the ElevateModal, prompts for the
// password, calls /api/auth/elevate, and resolves the promise true on
// success / false on cancel. The http<T> helper waits on that promise
// and transparently retries the original request when it resolves
// true. This pattern lets any caller benefit from the auto-retry
// without changing its signature.
//
// The detail string the backend returns is the marker; matching it
// exactly keeps unrelated 403s (genuine permission failures) flowing
// through the normal error path instead of triggering the modal.
let _onElevationRequired:
  | (() => Promise<boolean>)
  | null = null;

/**
 * Substring the backend's elevation gate puts in its 403 detail.
 * Kept as a const so tests and the modal share the canonical token.
 */
export const ELEVATION_REQUIRED_DETAIL_MARKER = 'recent password re-confirmation';

// ── Token persistence (no persistence) ──────────────────────────────
//
// The token lives ONLY in this module's in-memory closure for the
// lifetime of one mounted <App />. There is no localStorage,
// sessionStorage, or cookie write of the access token anywhere in
// the frontend.
//
// Reasoning: a "session" / "persistent" persistence mode would let
// the end user's browser sync (Chrome sync, etc.) replicate
// the token across devices, which breaks the basic safety property
// that "opening the app on a new device requires logging in." A
// page reload is also a fresh JS context - it correctly triggers
// re-login under this model. The Switch View Mode override is
// React state inside <AuthProvider>, so it resets on reload too -
// that's the intended security feature: dropped views never survive
// a JS-context restart.

/**
 * Push the access token into the api module's closure. ``null`` clears.
 *
 * Synchronous - call this BEFORE setting any React state that
 * triggers a re-render, so child components that mount and call the
 * API in their first effect see the token in the closure immediately.
 */
export function setAccessToken(token: string | null): void {
  _accessToken = token;
}

export function getAccessToken(): string | null {
  return _accessToken;
}

/**
 * Decode a JWT's payload **without verifying the
 * signature**. Returns ``null`` on any malformed input. Used by
 * App.tsx on page reload to recover the signed-in username + role
 * from a persisted token so the topbar's user chip + Log out button
 * stay visible across reloads.
 *
 * Signature verification stays a backend concern: the JWT secret
 * never reaches the browser, and every API request the frontend
 * issues is re-validated server-side. A token whose payload we can
 * decode but that the backend rejects gets cleared via the 401
 * handler at first request.
 */
export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    // JWT uses base64url; pad to a multiple of 4 and replace URL-safe
    // chars so the browser's atob accepts it.
    let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    const json = atob(b64);
    const obj = JSON.parse(json);
    return obj && typeof obj === 'object' ? (obj as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

export function onUnauthorized(handler: () => void): void {
  _onUnauthorized = handler;
}

/**
 * Register the elevation handler. App.tsx supplies a
 * function that opens the ElevateModal and resolves the returned
 * promise true after a successful POST /api/auth/elevate, or false
 * when the user cancels. Subsequent 403s carrying the elevation
 * marker call this handler and retry the original request on
 * resolved-true.
 */
export function onElevationRequired(
  handler: (() => Promise<boolean>) | null,
): void {
  _onElevationRequired = handler;
}

// Internal accessor for the unauthorized handler. The WebSocket
// clients (still in api.ts during Phase 2f) call this when a 4001
// close + refresh attempt fails so the app falls into the login
// branch via the same code path REST 401s use.
export function triggerUnauthorized(): void {
  if (_onUnauthorized) _onUnauthorized();
}

// ── REST helpers ─────────────────────────────────────────────────────────────
//
// Silent refresh: a single in-flight refresh promise is shared by every
// concurrent 401-retry so a burst of expired-token responses doesn't
// fan out into N refresh calls. The promise resolves to the new access
// token on success or null on failure.
let _refreshInFlight: Promise<string | null> | null = null;

/**
 * Run a single silent token refresh, coalescing concurrent callers
 * onto one in-flight promise. Used by both the REST 401 retry path
 * and the dashboard / dev-console WebSocket clients' 4001 reconnect
 * path. Exported so the WS clients (still living in ``api.ts`` for
 * Phase 2f) can reach it.
 */
export async function refreshOnce(): Promise<string | null> {
  if (_refreshInFlight) return _refreshInFlight;
  _refreshInFlight = (async () => {
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!res.ok) return null;
      const body = await res.json();
      const newToken = body && typeof body.access_token === 'string'
        ? body.access_token
        : null;
      if (newToken) _accessToken = newToken;
      return newToken;
    } catch {
      return null;
    } finally {
      // Reset on next tick so simultaneous callers all observe the
      // same resolved value, then a *future* 401 burst gets its own
      // fresh refresh attempt.
      setTimeout(() => { _refreshInFlight = null; }, 0);
    }
  })();
  return _refreshInFlight;
}

export async function http<T>(path: string, init?: RequestInit, _isRetry = false): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined || {}),
  };
  if (_accessToken) {
    headers['Authorization'] = `Bearer ${_accessToken}`;
  }
  const res = await fetch(path, {
    // Same-origin cookies (refresh_token at Path=/api/auth) must
    // accompany requests to that path. Default for same-origin
    // fetch is to include cookies, but pinning it explicitly here
    // protects against future browser changes.
    credentials: 'same-origin',
    ...init,
    headers,
  });
  if (res.status === 401) {
    // Auth endpoints (login, setup, refresh, verify-password) report
    // 401 legitimately - those callers want to see it. Anything else
    // is a mid-session expiry: try a silent refresh once, then
    // transparently retry the original request.
    const isAuthPath = path.startsWith('/api/auth/');
    if (!isAuthPath && !_isRetry) {
      const newToken = await refreshOnce();
      if (newToken) {
        return http<T>(path, init, true);
      }
      if (_onUnauthorized) _onUnauthorized();
    } else if (!isAuthPath && _isRetry && _onUnauthorized) {
      // Retry also returned 401 - refresh succeeded but the original
      // request was still rejected (role demotion, account deleted).
      // Fall through to the global handler.
      _onUnauthorized();
    }
  }
  if (!res.ok) {
    // A Response body is a one-shot stream. ``res.json()`` consumes
    // the stream even when it throws (e.g. on an HTML / plain-text
    // error body), which would make a later ``res.text()`` fall back
    // fail with "body stream already read". So read raw text once,
    // then try to parse as JSON to extract
    // ``detail``; either way the original text is available for the
    // error message.
    // ``detail`` is whatever FastAPI puts in the ``detail`` slot of
    // the JSON body. Some routes return a plain string, some return
    // a structured ``{code, message}`` object (the playlist-mgmt
    // routes do this for SMART_PLAYLIST_NOT_PORTABLE, PLAYLIST_NOT_FOUND,
    // DEST_USER_TOKEN_MISSING, etc.). We keep the raw value here so the
    // downstream Error message can format both shapes correctly. Without
    // this, structured detail ended up as ``[object Object]`` in the
    // thrown message because string-template coerces an object via
    // ``Object.prototype.toString``.
    let detail: string | Record<string, unknown> = '';
    try {
      const raw = await res.text();
      if (raw) {
        try {
          const body = JSON.parse(raw);
          if (body && typeof body === 'object' && 'detail' in body) {
            const inner = (body as { detail: unknown }).detail;
            if (typeof inner === 'string') {
              detail = inner;
            } else if (inner && typeof inner === 'object') {
              detail = inner as Record<string, unknown>;
            } else {
              detail = raw;
            }
          } else {
            detail = raw;
          }
        } catch {
          detail = raw;
        }
      }
    } catch {
      // Body couldn't even be read as text. Leave detail empty -
      // the status code + statusText below still tell the user
      // something useful.
    }
    // 403s carrying the elevation-required marker are
    // intercepted here. The handler opens the modal, captures the
    // password, calls /api/auth/elevate, and resolves true on
    // success. We then retry the original request once. A user
    // cancel (resolved false) falls through to the normal throw so
    // the caller sees a clean 403.
    //
    // The retry is gated on _isRetry so a misbehaving elevate flow
    // (where the second call still returns 403) cannot infinite-loop.
    // _isRetry is also already used by the 401-then-refresh path.
    if (
      res.status === 403
      && !_isRetry
      && _onElevationRequired
      && typeof detail === 'string'
      && detail.includes(ELEVATION_REQUIRED_DETAIL_MARKER)
    ) {
      try {
        const ok = await _onElevationRequired();
        if (ok) {
          return http<T>(path, init, true);
        }
      } catch {
        // Handler threw - fall through to the normal error throw
        // below so the caller sees the original 403 and the modal
        // closes via its own cancel path.
      }
    }
    // Format the message. Structured ``{code, message}`` detail (returned
    // by several /api/playlist-mgmt routes) is rendered as ``CODE: message``
    // so the end user-actionable string is readable in toasts and result
    // panels. When the detail carries both fields we additionally throw
    // a ``PlaylistMgmtStructuredError`` so callers can ``instanceof``-
    // dispatch on the code (e.g. handle DEST_USER_NOT_FOUND or
    // DEST_USER_TOKEN_MISSING specifically). Plain-text details fall
    // through to a regular ``Error`` so existing callers keep working.
    let detailText = '';
    let structuredCode: string | null = null;
    let structuredMessage: string | null = null;
    if (typeof detail === 'string') {
      detailText = detail;
    } else if (detail && typeof detail === 'object') {
      const code = typeof (detail as { code?: unknown }).code === 'string'
        ? (detail as { code: string }).code
        : null;
      const message = typeof (detail as { message?: unknown }).message === 'string'
        ? (detail as { message: string }).message
        : null;
      structuredCode = code;
      structuredMessage = message;
      if (code && message) detailText = `${code}: ${message}`;
      else if (message) detailText = message;
      else if (code) detailText = code;
      else {
        try { detailText = JSON.stringify(detail); }
        catch { detailText = '(unserializable detail)'; }
      }
    }
    if (structuredCode && structuredMessage) {
      throw new PlaylistMgmtStructuredError(
        res.status, structuredCode, structuredMessage,
      );
    }
    throw new Error(`${res.status} ${res.statusText}${detailText ? `: ${detailText}` : ''}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}
