"""
Headless-mode setup + stop-signal hub for the Hestia-MediaManager engine when
it runs inside the FastAPI server.

The engine in ``services/`` spawns a stop-watch daemon thread
(``services.dashboard._keyboard_thread``) at the top of every
``run_snapshot`` / ``run_restore`` / direct-transfer call. That thread
registers the run's ``stop_event`` on ``services.state._active_stop_event``
and blocks until the run ends.

This module is the server-side half of the stop mechanism:
:func:`signal_stop` and :func:`signal_hard_stop` read the registered
event so the ``/api/job/stop`` REST endpoint can flip the flag and the
engine winds the job down at its next checkpoint.

:func:`enable_headless_mode` sets ``services.state.HEADLESS_MODE`` so
``setup_logging`` skips its ``sys.excepthook`` rebind - uvicorn owns
that hook in server mode.

This module is imported once, from :mod:`server.app` at application
startup.
"""

from __future__ import annotations

import logging

import services.state as _state


def signal_stop() -> bool:
    """
    Request a clean shutdown of the currently running engine job.

    Returns ``True`` if a stop was signalled, ``False`` if no engine
    job is currently running.

    Logs an INFO line to the per-run "plexmigrate" logger so the
    message shows up in runtime.log while the user is tailing it from
    the web UI. The frontend's Stop button surfaces a "Stopping…"
    state as soon as JobQueue.request_stop flips the JobRecord; this
    log line gives the long-form story: the engine will exit at its
    next library boundary.
    """
    ev = _state._active_stop_event
    if ev is None:
        return False
    try:
        logging.getLogger("plexmigrate").info(
            "Stop requested - finishing current library and exiting."
        )
    except Exception:  # pragma: no cover (defensive)
        pass
    ev.set()
    return True


def signal_hard_stop() -> bool:
    """
    Force the running engine job to bail out as fast as it can (v0.12.1).

    Soft :func:`signal_stop` is cooperative - the engine drains pending
    futures and exits at the next library boundary, which can take
    seconds or minutes depending on what's in flight. A "hard" stop is
    for the case where that's too slow (long-running per-library
    operations, a misbehaving destination server) and the end user just
    wants the worker free *right now* even if some in-flight items end
    up in the failure log.

    What it actually does, in order:

    1. **Set the soft stop_event** - same flag :func:`signal_stop`
       flips, so any engine code that periodically checks
       ``stop_event.is_set()`` sees it.
    2. **Close the shared HTTP session** at
       :data:`services.state._session` - every in-flight Plex request
       running through it raises ``requests.exceptions.RequestException``
       as the underlying connection pool tears down. The engine's
       per-item ``try/except`` handlers catch those, record them as
       failures, and the per-library loop exits via the stop_event
       check.

    The actual cancellation of the JobRecord state (flipping to
    CANCELLED, marking the job runner free for the next submission)
    happens in :meth:`server.jobs.JobQueue.request_stop` with
    ``hard=True``. This function only signals the engine; it does NOT
    flip the JobRecord. Two-stage so the lock discipline in
    ``server.jobs`` stays the single owner of JobRecord transitions.

    Returns ``True`` if at least the soft signal was delivered.
    """
    ev = _state._active_stop_event
    if ev is None:
        return False
    try:
        logging.getLogger("plexmigrate").warning(
            "Hard stop requested - tearing down HTTP session; in-flight "
            "items will land in the failure log."
        )
    except Exception:  # pragma: no cover (defensive)
        pass
    # Step 1: cooperative flag for any engine loop already checking it.
    ev.set()
    # Step 2: yank the HTTP session out from under in-flight requests.
    # ``requests.Session.close`` releases pooled connections without
    # raising on its own. Concurrent ``session.get(...)`` calls already
    # in flight will fail because the urllib3 pool got torn down -
    # engine code handles those at the per-item level so the job
    # collapses rather than hanging.
    try:
        sess = _state._session
        if sess is not None:
            sess.close()
    except Exception:  # pragma: no cover (defensive)
        pass
    return True


# ── Public entry point ───────────────────────────────────────────────────────

def enable_headless_mode() -> None:
    """
    Mark the engine as running headless (inside uvicorn). Idempotent.

    Sets ``services.state.HEADLESS_MODE`` so ``setup_logging`` skips
    the ``sys.excepthook`` rebind - uvicorn owns that hook in server
    mode. :func:`server.app.create_app` calls this at module import
    time so the order is guaranteed before any engine run.
    """
    _state.HEADLESS_MODE = True
