"""
Rule 2 - Library-walk job. Periodically (and on-demand) confirm which
items are still present on each registered server so the end user can
identify and prune items that have permanently disappeared.

Why this exists
---------------
media.db never auto-deletes based on item absence. A library scan that
returns a thinner-than-expected response could mean the file was moved,
the library scan missed an item, or the server was briefly degraded -
none of those should drop the row from our cache. So instead of
deletion we have a positive sighting trail: every walk ticks
``server_items.last_seen_at`` for every item the server reports
present. Items the walk hasn't seen recently become candidates for the
end user-confirmed Prune Missing Items action (see
``server/media_db.py :: prune_stale_items``).

Two entry points
----------------
* :func:`run_walk_once` - walks a single server now. Used by the
  "Run library walk now" button in the Servers panel and by the
  scheduler when its tick fires.
* :func:`LibraryWalkScheduler` - daemon thread that fires
  :func:`run_walk_once` for every registered server at the configured
  cadence (default 24h). Started at server boot from
  ``server/app.py`` lifespan.

Mutual exclusion
----------------
Only one walk per (server_id) at a time. A second concurrent attempt
returns the already-running walk's id and exits immediately. Walks on
DIFFERENT servers can run in parallel.

Error handling
--------------
The walk is best-effort. Per-section / per-item errors are logged
and counted but never abort the walk; the run finishes with
status='completed' even if some sections failed (the libraries_seen
counter reflects how many succeeded). A walk that can't even connect
to the server finishes with status='failed' and an error_message.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

from plexapi.server import PlexServer

from server import media_db


log = logging.getLogger("plexmigrate.server.library_walk")


# Global registry of currently-running walks: server_id → walk_id.
# A lock guards the map for correctness; individual walks are
# otherwise independent.
_running: Dict[str, int] = {}
_running_lock = threading.Lock()


def is_walk_running(server_id: str) -> bool:
    """True iff there's an active walk for this server."""
    with _running_lock:
        return server_id in _running


def _register_walk(server_id: str) -> tuple:
    """
    Reserve a walk slot for ``server_id``. Returns
    ``(walk_id, is_duplicate)``: when a walk is already running for
    this server the in-flight walk's id is returned with
    ``is_duplicate=True`` and no new ``library_walks`` row is created.
    """
    with _running_lock:
        if server_id in _running:
            log.info(
                "Library walk for %r already running (walk_id=%d); "
                "skipping duplicate trigger.",
                server_id, _running[server_id],
            )
            return _running[server_id], True
        walk_id = media_db.start_library_walk(server_id)
        _running[server_id] = walk_id
        return walk_id, False


def run_walk_once(
    *,
    server_id: str,
    plex: PlexServer,
) -> Dict[str, int]:
    """
    Walk every visible library on ``plex`` and tick
    ``server_items.last_seen_at`` for each item the server reports.

    Synchronous - the caller's thread runs the whole walk. Used by the
    scheduler (which already owns a background thread). On-demand HTTP
    triggers use :func:`start_walk_background` instead so they don't
    block a uvicorn worker for the walk's duration.

    Returns ``{walk_id, items_seen, libraries_seen, sections_failed}``.
    Idempotent under concurrent calls: a second call while a walk is
    already running for this server returns the in-flight walk's id
    with zeroed counters and exits immediately.
    """
    walk_id, is_duplicate = _register_walk(server_id)
    if is_duplicate:
        return {
            "walk_id": walk_id,
            "items_seen": 0,
            "libraries_seen": 0,
            "sections_failed": 0,
            "skipped_duplicate": 1,
        }
    return _run_walk_body(walk_id, server_id, plex)


def _run_walk_body(
    walk_id: int, server_id: str, plex: PlexServer,
) -> Dict[str, int]:
    """
    Execute the walk for an already-registered ``walk_id`` (the slot
    must have been reserved via :func:`_register_walk`). Always
    finishes the ``library_walks`` row and clears the ``_running``
    entry, even on failure.
    """
    items_seen = 0
    libraries_seen = 0
    sections_failed = 0
    error_message: Optional[str] = None
    walk_at = time.time()

    try:
        sections = list(plex.library.sections())
        log.info(
            "Library walk %d started for server %r (%d sections)",
            walk_id, server_id, len(sections),
        )
        for section in sections:
            try:
                # Mirror resolver._section_leaf_items so we tick at the
                # same granularity the resolver caches. Movies are
                # walked at the Movie level; Music at the Track level;
                # TV at the Episode level. Library types we don't know
                # how to walk (Photos, etc.) iterate via .all().
                libtype = getattr(section, "type", "")
                if libtype == "artist":
                    iterator = section.searchTracks()
                elif libtype == "show":
                    iterator = section.searchEpisodes()
                else:
                    iterator = section.all()
                for item in iterator:
                    rk = getattr(item, "ratingKey", None)
                    if rk is None:
                        continue
                    try:
                        if media_db.record_item_sighting(
                            server_id=server_id,
                            rating_key=int(rk),
                            walk_at=walk_at,
                        ) is not None:
                            items_seen += 1
                    except Exception:
                        # Per-item ticks are non-critical - keep going.
                        continue
                libraries_seen += 1
            except Exception as section_exc:
                log.warning(
                    "Library walk %d: section %r failed: %s",
                    walk_id, getattr(section, "title", "?"), section_exc,
                )
                sections_failed += 1
                continue
        status = "completed"
    except Exception as walk_exc:
        log.exception(
            "Library walk %d for server %r failed at top level",
            walk_id, server_id,
        )
        status = "failed"
        error_message = f"{type(walk_exc).__name__}: {walk_exc}"
    finally:
        try:
            media_db.finish_library_walk(
                walk_id,
                status=status,
                items_seen=items_seen,
                libraries_seen=libraries_seen,
                error_message=error_message,
            )
        except Exception:
            log.exception("finish_library_walk failed for walk %d", walk_id)
        with _running_lock:
            _running.pop(server_id, None)

    log.info(
        "Library walk %d finished for server %r: %d items seen across "
        "%d section(s) (%d failed) status=%s",
        walk_id, server_id, items_seen, libraries_seen,
        sections_failed, status,
    )
    return {
        "walk_id": walk_id,
        "items_seen": items_seen,
        "libraries_seen": libraries_seen,
        "sections_failed": sections_failed,
    }


def start_walk_background(
    *,
    server_id: str,
    url: str,
    token: str,
) -> Dict[str, int]:
    """
    Reserve a walk slot and run the walk in a daemon thread, so an
    on-demand HTTP trigger returns immediately.

    M11: a library walk on a large server takes minutes. Running it
    inline (``PlexServer(...)`` connect + ``run_walk_once``) blocked a
    uvicorn worker for the whole duration, and concurrent triggers
    could exhaust the worker pool. The Plex connect happens inside the
    background thread too; a connect failure marks the walk row
    ``failed`` rather than 502-ing the request.

    Returns ``{walk_id, started}`` right away - the caller polls
    ``GET /library-walk`` (``is_walk_running`` + ``list_library_walks``)
    for progress. A duplicate trigger returns the in-flight walk's id
    with ``started=0``.
    """
    walk_id, is_duplicate = _register_walk(server_id)
    if is_duplicate:
        return {"walk_id": walk_id, "started": 0, "skipped_duplicate": 1}

    def _worker() -> None:
        try:
            plex = PlexServer(url, token, timeout=120)
        except Exception as exc:
            log.warning(
                "Library walk %d: cannot connect to server %r (%s); "
                "marking walk failed.",
                walk_id, server_id, exc,
            )
            try:
                media_db.finish_library_walk(
                    walk_id,
                    status="failed",
                    items_seen=0,
                    libraries_seen=0,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                log.exception("finish_library_walk failed for walk %d", walk_id)
            with _running_lock:
                _running.pop(server_id, None)
            return
        _run_walk_body(walk_id, server_id, plex)

    threading.Thread(
        target=_worker,
        name=f"library-walk-{server_id}",
        daemon=True,
    ).start()
    return {"walk_id": walk_id, "started": 1}


# ── Scheduler ──────────────────────────────────────────────────────────────
#
# Daemon thread that walks every registered server at a configurable
# cadence. Reads ``settings.library_walk`` for the interval; defaults
# to 86400 seconds (24h) when missing. A walk fires for every server
# at startup (if last walk was longer ago than the interval) and then
# on the interval thereafter.


_DEFAULT_INTERVAL_SECONDS = 86400.0  # 24h
_MIN_INTERVAL_SECONDS = 3600.0       # 1h floor - shorter cadences thrash large libraries


class LibraryWalkScheduler:
    """
    Single-process daemon that fires the library walk for each
    registered server on a configurable cadence. Calling ``stop()``
    signals the loop; the thread exits at the next tick boundary.
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="library-walk-scheduler", daemon=True,
        )
        self._thread.start()
        log.info("LibraryWalkScheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        log.info("LibraryWalkScheduler stop requested")

    def _read_interval(self) -> float:
        try:
            from server.persistence import load_settings
            cfg = (load_settings() or {}).get("library_walk") or {}
            raw = cfg.get("interval_seconds")
            if raw is None:
                return _DEFAULT_INTERVAL_SECONDS
            interval = float(raw)
            if interval < _MIN_INTERVAL_SECONDS:
                return _MIN_INTERVAL_SECONDS
            return interval
        except Exception:
            return _DEFAULT_INTERVAL_SECONDS

    def _is_enabled(self) -> bool:
        try:
            from server.persistence import load_settings
            cfg = (load_settings() or {}).get("library_walk") or {}
            return bool(cfg.get("enabled", True))
        except Exception:
            return True

    def _loop(self) -> None:
        # First tick: short delay so startup isn't slowed by a walk
        # that may take minutes on a large server. The scheduler then
        # settles into its configured cadence.
        initial_delay = 60.0
        if self._stop_event.wait(initial_delay):
            return
        while not self._stop_event.is_set():
            if self._is_enabled():
                try:
                    self._tick()
                except Exception:
                    log.exception("LibraryWalkScheduler tick failed")
            else:
                log.debug("LibraryWalkScheduler: walks disabled, skipping tick")
            # Re-read the interval each tick so a Settings change is
            # honoured without restarting the server.
            interval = self._read_interval()
            if self._stop_event.wait(interval):
                return

    def _tick(self) -> None:
        from server import server_registry
        try:
            servers = server_registry.list_servers(include_tokens=True)
        except Exception:
            log.exception("LibraryWalkScheduler: server_registry.list_servers failed")
            return
        interval = self._read_interval()
        cutoff = time.time() - interval
        for srv in servers or []:
            server_id = srv.get("id")
            if not server_id:
                continue
            # Skip if the last walk for this server finished more
            # recently than the interval - prevents a server boot
            # bouncing fires.
            last = media_db.get_last_walk_summary(server_id)
            if last and (last.get("started_at") or 0.0) > cutoff:
                continue
            url = srv.get("url") or ""
            token = srv.get("token") or ""
            if not (url and token):
                continue
            try:
                plex = PlexServer(url, token, timeout=120)
            except Exception as exc:
                log.warning(
                    "LibraryWalkScheduler: cannot connect to %r (%s); skipping walk.",
                    server_id, exc,
                )
                continue
            try:
                run_walk_once(server_id=server_id, plex=plex)
            except Exception:
                log.exception("LibraryWalkScheduler: walk failed for %r", server_id)


# Module-level singleton so the FastAPI lifespan can start/stop one
# scheduler per process.
scheduler = LibraryWalkScheduler()
