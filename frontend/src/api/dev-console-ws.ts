// Dev-console WebSocket client extracted from the monolithic
// ``frontend/src/api.ts`` during Phase 3c. Lifecycle and reconnect
// behaviour are byte-for-byte the same as the legacy class; only the
// file boundary moved.
//
// Separate channel from the dashboard socket. Pushes a discrete event
// the moment a console command completes, plus a heartbeat scoped to
// the server the panel is viewing (the client reports it via
// setWatch). Root-admin only - the backend handshake rejects non-root
// tokens (close 4003) and a disabled tunable.

import type { DevConsoleEvent } from './types';
import { buildWsUrl } from './ws-base';

type DevConsoleListener = (ev: DevConsoleEvent) => void;

export class DevConsoleWsClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<DevConsoleListener>();
  private reconnectTimer: number | null = null;
  private retryDelayMs = 1000;
  private readonly maxDelayMs = 10_000;
  private explicitlyClosed = false;
  // The server the panel is currently viewing. Sent to the backend so
  // the heartbeat is scoped to that server; re-sent on every (re)connect.
  private watchedServer = '';

  subscribe(fn: DevConsoleListener): () => void {
    this.listeners.add(fn);
    this.ensureConnected();
    return () => {
      this.listeners.delete(fn);
      if (this.listeners.size === 0) this.close();
    };
  }

  /** Tell the backend which server this panel is viewing so the
   * heartbeat is scoped to it. */
  setWatch(serverId: string) {
    this.watchedServer = serverId || '';
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify({ watch: this.watchedServer }));
      } catch {
        // Send failed; the next reconnect's onopen re-sends it.
      }
    }
  }

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
    if (
      this.ws
      && (this.ws.readyState === WebSocket.OPEN
        || this.ws.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    this.explicitlyClosed = false;
    const ws = new WebSocket(buildWsUrl('/ws/dev-console'));
    this.ws = ws;

    ws.onopen = () => {
      // Re-assert the watched server so the heartbeat scopes correctly
      // after a reconnect.
      if (this.watchedServer) {
        try {
          ws.send(JSON.stringify({ watch: this.watchedServer }));
        } catch {
          // Ignore; a later setWatch / reconnect will retry.
        }
      }
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as DevConsoleEvent;
        this.listeners.forEach((l) => l(msg));
        this.retryDelayMs = 1000;
      } catch {
        // Ignore malformed frames.
      }
    };

    ws.onclose = (ev) => {
      this.ws = null;
      if (this.explicitlyClosed || this.listeners.size === 0) return;
      // 4003 = forbidden (non-root or tunable off). No point
      // reconnecting - the console will not render for this user.
      if (ev.code === 4003) {
        this.explicitlyClosed = true;
        return;
      }
      this.reconnectTimer = window.setTimeout(() => {
        this.reconnectTimer = null;
        this.retryDelayMs = Math.min(this.retryDelayMs + 1000, this.maxDelayMs);
        this.ensureConnected();
      }, this.retryDelayMs);
    };

    ws.onerror = () => {
      // onclose fires right after and schedules the retry.
    };
  }
}

export const devConsoleWsClient = new DevConsoleWsClient();
