"""
Persistent, server-keyed HTTP telemetry collector (v0.12.0).

Background
----------
Pre-v0.12.0, network telemetry (status-code counts, rolling RPS /
latency series, 429 retry-after events) lived inside the per-job
:class:`services.dashboard.DashboardState`. That coupling made the
Network panel disappear in two scenarios:

* **Idle.** No job → no DashboardState → no telemetry surface, even
  though the registered servers are still pingable.
* **Fan-out.** Each destination has its own DashboardState; there's
  no single "the" dashboard to embed Network details on, so the
  fan-out layout omitted the panel entirely.

This module replaces the per-job substrate with a process-lifetime,
**server-keyed** collector. Every HTTP response the engine fires -
regardless of which job, which destination thread, or whether a job
is running at all - feeds in. Idle ping-poll results feed in too.
The Networking tab reads from here and stays correct in every UI
state.

Scope: telemetry only. The collector never sees auth tokens, request
bodies, or response payloads - just URL host, status code, elapsed
time, and the registered-server id it resolves to.

Resolution
----------
The collector identifies servers by the **host[:port] of the request
URL**, then maps that to a registered ``server_id`` via the registry.
Engine code doesn't have to set any context - it just keeps making
requests, and the hook in :mod:`services.auth` reports the URL.

Bucketing
---------
Per-server state lives in :class:`_ServerBucket`:

* ``latency_ring``  - last N response timings (timestamp + ms).
* ``status_counts`` - cumulative-since-process-start counter per
  status code (200, 401, 429, 5xx, …).
* ``rate_limits``   - last N 429 / 503 events with retry-after.
* ``last_ping_ms``  - most recent ping latency (idle telemetry).
* ``last_seen_at``  - UNIX timestamp of last response or ping.

Ring buffers cap at 600 entries - enough to compute a 60-second
rolling window even under sustained 10 RPS - without unbounded growth
on a long-lived process. Aging is timestamp-based, not count-based,
so a quiet server doesn't fall out of the window.

Thread safety
-------------
The collector is touched from every HTTP-firing thread (engine
worker pools, fan-out destinations, the ping poller). Each
``_ServerBucket`` has a per-instance lock; the outer dict mapping
host → bucket is guarded by a separate lock for create/lookup races.
Snapshot reads acquire the bucket lock briefly per server.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse


log = logging.getLogger("plexmigrate.server.network_collector")


# Ring-buffer caps. Latency/RPS windows compute over the last 60 s of
# real time; the count cap is a defence against memory growth if a
# poorly-rate-limited workload manages >10 RPS sustained against one
# server. At 600 entries × ~80 bytes per ring entry that's ~50 KB per
# server - entirely negligible.
_LATENCY_RING_CAP = 600
_RATE_LIMIT_RING_CAP = 64
_WINDOW_SECONDS = 60.0
# Number of one-second buckets emitted in the ``series`` field of the
# per-server snapshot. The Dashboard inline Network panel uses this
# series both for the line chart (full 60-bucket render) and for the
# 3-second "instantaneous" tile values (last 3 entries). Independent
# of ``_LATENCY_RING_CAP`` because the series is *aggregated* (one
# entry per second regardless of raw sample volume).
_SERIES_BUCKETS = 60


@dataclass
class _LatencyEntry:
    """One HTTP response timing record."""
    timestamp: float
    elapsed_ms: float
    status_code: int


@dataclass
class _RateLimitEntry:
    """One 429 / 503 event surfaced into the Network panel's feed."""
    timestamp: float
    status_code: int
    retry_after_seconds: Optional[float]


@dataclass
class _ServerBucket:
    """
    Per-server telemetry. Updates from multiple threads are serialised
    via :attr:`lock`. Reads from the snapshot path also take the lock
    briefly - the inner work is dict / deque iteration, microseconds.
    """
    host: str
    latency_ring: Deque[_LatencyEntry] = field(default_factory=lambda: deque(maxlen=_LATENCY_RING_CAP))
    rate_limit_ring: Deque[_RateLimitEntry] = field(default_factory=lambda: deque(maxlen=_RATE_LIMIT_RING_CAP))
    status_counts: Dict[int, int] = field(default_factory=dict)
    last_ping_ms: Optional[float] = None
    last_ping_ok: bool = False
    last_ping_at: float = 0.0
    last_seen_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_response(self, elapsed_ms: float, status_code: int,
                        retry_after_seconds: Optional[float], now: float) -> None:
        with self.lock:
            self.latency_ring.append(_LatencyEntry(
                timestamp=now, elapsed_ms=elapsed_ms, status_code=status_code,
            ))
            self.status_counts[status_code] = self.status_counts.get(status_code, 0) + 1
            if status_code in (429, 503):
                self.rate_limit_ring.append(_RateLimitEntry(
                    timestamp=now,
                    status_code=status_code,
                    retry_after_seconds=retry_after_seconds,
                ))
            self.last_seen_at = now

    def record_ping(self, ok: bool, elapsed_ms: Optional[float], now: float) -> None:
        with self.lock:
            self.last_ping_ms = elapsed_ms if elapsed_ms is not None and elapsed_ms >= 0 else None
            self.last_ping_ok = ok
            self.last_ping_at = now
            self.last_seen_at = now

    def to_dict(self, now: float) -> Dict[str, Any]:
        """
        Build a JSON-safe payload describing this server's telemetry
        over the trailing 60-second window. Aging is timestamp-based:
        entries older than ``_WINDOW_SECONDS`` are dropped from the
        rolling-window aggregates (but kept in cumulative counters so
        the operator can still see "this server returned 47 429s
        since the process started" elsewhere).
        """
        with self.lock:
            cutoff = now - _WINDOW_SECONDS
            # Window-scoped latency stats.
            window_entries = [e for e in self.latency_ring if e.timestamp >= cutoff]
            n = len(window_entries)
            if n > 0:
                avg_ms = sum(e.elapsed_ms for e in window_entries) / n
                rps = n / _WINDOW_SECONDS
            else:
                avg_ms = None
                rps = 0.0
            # Recent rate-limit events (still trimmed by window so the
            # feed clears once Plex stops throttling).
            recent_rls = [
                {
                    "timestamp": e.timestamp,
                    "status_code": e.status_code,
                    "retry_after_seconds": e.retry_after_seconds,
                }
                for e in self.rate_limit_ring if e.timestamp >= cutoff
            ]
            # Window-scoped status histogram so the UI can show "in
            # the last minute: 87% 200, 13% 429" without the operator
            # mentally subtracting old cumulative counts.
            window_status: Dict[str, int] = {}
            for e in window_entries:
                k = str(e.status_code)
                window_status[k] = window_status.get(k, 0) + 1
            # Per-second time series: one bucket per second covering
            # the trailing _SERIES_BUCKETS seconds. Each entry is
            # ``{t, rps, avg_ms}`` where ``rps`` is the count of
            # samples that landed in that second (so RPS = count per
            # 1-second bucket) and ``avg_ms`` is the mean latency
            # across those samples, or ``null`` for empty buckets.
            #
            # The Dashboard's inline NetworkPanel reads this to draw
            # the line chart and to compute its 3-second instantaneous
            # tile values. The standalone Networking tab keeps using
            # the rolling-60s rps/avg_ms fields above and ignores
            # this field - additive only.
            now_sec = int(now)
            start_sec = now_sec - _SERIES_BUCKETS + 1
            counts: List[int] = [0] * _SERIES_BUCKETS
            totals_ms: List[float] = [0.0] * _SERIES_BUCKETS
            for e in self.latency_ring:
                sec = int(e.timestamp)
                idx = sec - start_sec
                if 0 <= idx < _SERIES_BUCKETS:
                    counts[idx] += 1
                    totals_ms[idx] += e.elapsed_ms
            series = []
            for i in range(_SERIES_BUCKETS):
                c = counts[i]
                series.append({
                    "t": start_sec + i,
                    "rps": c,
                    "avg_ms": (totals_ms[i] / c) if c > 0 else None,
                })
            return {
                "host": self.host,
                "rps": rps,
                "avg_ms": avg_ms,
                "last_ping_ms": self.last_ping_ms,
                "last_ping_ok": self.last_ping_ok,
                "last_ping_at": self.last_ping_at,
                "last_seen_at": self.last_seen_at,
                "window_status_counts": window_status,
                "cumulative_status_counts": dict(self.status_counts),
                "rate_limit_events": recent_rls,
                "sample_count_in_window": n,
                "series": series,
            }


# ── Module singleton ─────────────────────────────────────────────────────────

_buckets_lock = threading.Lock()
_buckets: Dict[str, _ServerBucket] = {}


def _normalise_host(url_or_host: str) -> Optional[str]:
    """
    Reduce a URL or raw ``host[:port]`` string to the comparable form
    used as the bucket key. Returns ``None`` if the input has no host
    component (an unfortunate but defensive case for malformed URLs).

    Examples:
        ``http://192.168.1.10:32400/path`` → ``192.168.1.10:32400``
        ``192.168.1.10:32400``              → ``192.168.1.10:32400``
        ``http://plex.local/``              → ``plex.local``
    """
    if not url_or_host:
        return None
    if "://" in url_or_host:
        parsed = urlparse(url_or_host)
        host = parsed.netloc
    else:
        # Treat as raw host[:port]. Strip leading/trailing whitespace
        # and any trailing slash a caller might have left on.
        host = url_or_host.strip().rstrip("/")
    return host.lower() or None


def _get_or_create_bucket(host: str) -> _ServerBucket:
    """
    Return the bucket for ``host``, creating it on first encounter.
    Cheap path: read-mostly dict lookup. Write path acquires the
    outer lock only when a new bucket has to be inserted.
    """
    bucket = _buckets.get(host)
    if bucket is not None:
        return bucket
    with _buckets_lock:
        bucket = _buckets.get(host)
        if bucket is None:
            bucket = _ServerBucket(host=host)
            _buckets[host] = bucket
        return bucket


# ── Public API ───────────────────────────────────────────────────────────────

def record_response(
    url: str,
    status_code: int,
    elapsed_ms: float,
    retry_after_seconds: Optional[float] = None,
) -> None:
    """
    Report one HTTP response to the collector. Called from the
    response hook in :mod:`services.auth` for every Plex API call.

    Best-effort: any failure here is swallowed by the caller's
    try/except so telemetry never breaks the response path.
    """
    host = _normalise_host(url)
    if host is None:
        return
    _get_or_create_bucket(host).record_response(
        elapsed_ms=elapsed_ms,
        status_code=int(status_code),
        retry_after_seconds=retry_after_seconds,
        now=time.time(),
    )


def record_ping(url: str, ok: bool, elapsed_ms: Optional[float]) -> None:
    """
    Report one lightweight ping result. Called by
    :func:`server.server_registry.ping_server` so the Networking tab
    has data even when no job is running - that's the whole point of
    the v0.12.0 refactor.
    """
    host = _normalise_host(url)
    if host is None:
        return
    _get_or_create_bucket(host).record_ping(ok=ok, elapsed_ms=elapsed_ms, now=time.time())


def collect_network_state() -> List[Dict[str, Any]]:
    """
    Return a list of per-host snapshots. The WebSocket broadcaster
    pairs these with the registered-server list (joining by host) so
    the frontend gets ``{server_id, name, ...telemetry}`` per server.
    Hosts the collector saw HTTP traffic for but that aren't currently
    registered (legacy entries, deleted-mid-flight) still appear -
    the broadcaster decides whether to surface them.
    """
    now = time.time()
    with _buckets_lock:
        keys = list(_buckets.keys())
    return [_buckets[k].to_dict(now) for k in keys]


def snapshot_for_host(url_or_host: str) -> Optional[Dict[str, Any]]:
    """Targeted snapshot for one host. Returns ``None`` if untouched."""
    host = _normalise_host(url_or_host)
    if host is None:
        return None
    bucket = _buckets.get(host)
    if bucket is None:
        return None
    return bucket.to_dict(time.time())


def reset_all() -> None:
    """
    Discard every bucket. Intended for tests and for an operator
    "clear telemetry" action; not called from any production path.
    """
    with _buckets_lock:
        _buckets.clear()
