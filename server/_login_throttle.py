"""In-memory per-IP sliding-window throttle for the login endpoint.

Single-process, RLock-guarded. Sized for small operator deployments
(one container, low single-digit concurrent admins). Not a substitute
for a real WAF; complements the per-username lockout in auth_db.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional


class LoginThrottle:
    """Rolling-window failure counter per client IP.

    Parameters:
      window_seconds   - how far back failures count toward the cap.
      max_failures     - cap of failed attempts inside the window.
      cooldown_seconds - how long the IP stays blocked after the cap is hit.
    """

    def __init__(
        self,
        window_seconds: float = 300.0,
        max_failures: int = 10,
        cooldown_seconds: float = 900.0,
    ) -> None:
        self._window = window_seconds
        self._cap = max_failures
        self._cooldown = cooldown_seconds
        self._failures: Dict[str, Deque[float]] = {}
        self._blocked_until: Dict[str, float] = {}
        self._lock = threading.RLock()

    def check(self, ip: str) -> Optional[float]:
        """Return retry_after_seconds if this IP is blocked, else None."""
        if not ip:
            return None
        now = time.time()
        with self._lock:
            blocked_until = self._blocked_until.get(ip)
            if blocked_until and blocked_until > now:
                return blocked_until - now
            if blocked_until:
                # Cooldown expired; clear the entry.
                del self._blocked_until[ip]
            return None

    def record_failure(self, ip: str) -> Optional[float]:
        """Record one failed attempt. Returns retry_after_seconds if this
        attempt tipped the IP into the blocked state, else None."""
        if not ip:
            return None
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            dq = self._failures.setdefault(ip, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            dq.append(now)
            if len(dq) >= self._cap:
                self._blocked_until[ip] = now + self._cooldown
                dq.clear()
                return self._cooldown
            return None

    def record_success(self, ip: str) -> None:
        """Clear failure history for an IP after a successful login."""
        if not ip:
            return
        with self._lock:
            self._failures.pop(ip, None)
            self._blocked_until.pop(ip, None)


_DEFAULT_THROTTLE = LoginThrottle()


def get_login_throttle() -> LoginThrottle:
    return _DEFAULT_THROTTLE
