// Shared WebSocket foundation extracted from the legacy
// ``frontend/src/api.ts`` during Phase 2f. The dashboard and
// dev-console WS clients still live in ``api.ts`` (Phase 3 will split
// them into their own modules); what this module owns is the small
// set of primitives both of those clients sit on:
//
//   * ``buildWsUrl`` - canonical "same-origin + access-token query
//     string" URL construction. Browsers can't send custom headers on
//     a WebSocket handshake, so the JWT rides on ``?token=<jwt>`` and
//     the backend route handler validates it before accepting the
//     connection.
//   * ``DEFAULT_WS_INITIAL_RETRY_MS`` / ``DEFAULT_WS_MAX_RETRY_MS`` -
//     the linear-backoff bounds both clients use for non-auth
//     reconnect scheduling.
//
// No connection lifecycle is owned here in 2f - the per-resource
// clients each manage their own ``WebSocket`` instance. This module
// is the leaf both depend on; the auth-token side comes through
// ``./core`` via ``getAccessToken``.

import { getAccessToken } from './core';

/**
 * Initial linear-backoff delay (ms) before reconnecting after a
 * non-auth close. Matches the value both legacy WS classes use as
 * their starting retryDelayMs.
 */
export const DEFAULT_WS_INITIAL_RETRY_MS = 1000;

/**
 * Ceiling for the linear-backoff retry delay (ms). Matches the
 * ``maxDelayMs`` both legacy WS classes use.
 */
export const DEFAULT_WS_MAX_RETRY_MS = 10_000;

/**
 * Build a same-origin WebSocket URL for the given backend path,
 * appending ``?token=<access_token>`` when one is currently stored
 * in the api/core closure. Works in dev (Vite proxy) and prod
 * (nginx proxy).
 *
 * ``path`` should start with a leading slash, e.g. ``/ws/dashboard``.
 */
export function buildWsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const token = getAccessToken();
  const qs = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${proto}://${window.location.host}${path}${qs}`;
}
