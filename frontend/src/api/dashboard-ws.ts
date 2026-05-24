// Dashboard WebSocket client extracted from the monolithic
// ``frontend/src/api.ts`` during Phase 3c. Lifecycle, reconnect, and
// silent-refresh-on-4001 behaviour are byte-for-byte the same as the
// legacy class; only the file boundary moved.

import { refreshOnce, triggerUnauthorized } from './core';
import type { DashboardFrame } from './types';
import { buildWsUrl } from './ws-base';

type SocketListener = (snap: DashboardFrame) => void;

/**
 * Owns the WebSocket connection to the dashboard endpoint.
 *
 * Single instance, lazy-connect on the first .subscribe() call.
 * Automatically reconnects with linear backoff if the socket drops
 * (the user's job may still be running; we don't want to leave them
 * watching a frozen panel).
 */
export class DashboardWsClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<SocketListener>();
  private reconnectTimer: number | null = null;
  private retryDelayMs = 1000;
  private readonly maxDelayMs = 10_000;
  private explicitlyClosed = false;
  // Tracks whether the *next* close-code-4001 should be treated as a
  // hard auth failure rather than a "token might just be expired"
  // event. Set to true after we silently refresh and reconnect in
  // response to a 4001; cleared the moment we receive any successful
  // frame on the new connection. If the post-refresh connection
  // immediately 4001s again, the refresh produced a token the server
  // still rejects - that's a real auth failure and we surrender to
  // the login screen.
  private postRefreshReconnect = false;

  subscribe(fn: SocketListener): () => void {
    this.listeners.add(fn);
    this.ensureConnected();
    return () => {
      this.listeners.delete(fn);
    };
  }

  /** Force-close the socket. Call from React cleanup on full unmount. */
  close() {
    this.explicitlyClosed = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  private ensureConnected() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this.explicitlyClosed = false;
    // Same-origin URL - works in dev (Vite proxy) and prod (nginx proxy).
    // Append ``?token=<jwt>`` when an access token is set.
    // Browsers can't send custom headers on a WebSocket handshake, so
    // the token rides on the query string and the backend route
    // handler validates it before accepting the connection.
    const url = buildWsUrl('/ws/dashboard');
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as DashboardFrame;
        this.listeners.forEach((l) => l(msg));
        // A successful frame resets both the retry backoff and the
        // post-refresh sentinel - the connection is healthy now, so
        // a future 4001 (e.g. another 30 min from now) should once
        // again get the refresh-and-reconnect treatment.
        this.retryDelayMs = 1000;
        this.postRefreshReconnect = false;
      } catch {
        // Ignore malformed frames.
      }
    };

    ws.onclose = (ev) => {
      this.ws = null;
      if (this.explicitlyClosed || this.listeners.size === 0) return;
      // Close code 4001 = "auth required / failed" (set by the
      // backend handshake when the ?token=<jwt> is missing or
      // rejected). With the 30-minute access token TTL this is
      // the expected path when an idle session crosses the expiry
      // line - we attempt a silent refresh and reconnect at once,
      // bypassing the normal linear backoff so the end user sees
      // at most a 1-2 second blip instead of waiting for the next
      // poll cycle to invalidate-and-refresh the token.
      if (ev.code === 4001) {
        if (this.postRefreshReconnect) {
          // We already refreshed once for this cycle and the new
          // connection ALSO 4001s. That's a real auth failure -
          // give up and bounce to login.
          this.postRefreshReconnect = false;
          this.explicitlyClosed = true;
          triggerUnauthorized();
          return;
        }
        // Fire-and-forget the refresh; reconnect when it lands.
        this.postRefreshReconnect = true;
        refreshOnce().then((newToken) => {
          if (this.explicitlyClosed || this.listeners.size === 0) return;
          if (!newToken) {
            // Refresh cookie is gone or invalid - drop to login.
            this.postRefreshReconnect = false;
            this.explicitlyClosed = true;
            triggerUnauthorized();
            return;
          }
          // Immediate reconnect with the new token. The new value is
          // already in the api/core access-token closure (set inside
          // refreshOnce), so ensureConnected() will pick it up when
          // it builds the URL via buildWsUrl().
          this.ensureConnected();
        });
        return;
      }
      // Non-auth close - schedule a reconnect with linear backoff.
      this.reconnectTimer = window.setTimeout(() => {
        this.reconnectTimer = null;
        this.retryDelayMs = Math.min(this.retryDelayMs + 1000, this.maxDelayMs);
        this.ensureConnected();
      }, this.retryDelayMs);
    };

    ws.onerror = () => {
      // The browser will fire onclose right after this - handler there
      // schedules the retry. We don't double-schedule here.
    };
  }
}

export const dashboardWsClient = new DashboardWsClient();
