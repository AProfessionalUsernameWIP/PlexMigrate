"""
Headless-mode monkey patches for the PlexMigrate engine.

The engine in ``services/`` was originally written for an interactive
terminal. It branches on :func:`services.dashboard._check_terminal_size`
to decide whether to instantiate :class:`DashboardState` (the live,
WebSocket-streamable state model) or fall back to simple Rich Progress
bars. It also spawns :func:`services.dashboard._keyboard_thread` to
read raw keypresses for the [Q]uit / [V]erbose / [P]ause shortcuts.

When the engine is invoked from inside the FastAPI server we always
want:

1. The full ``DashboardState`` path — that is the only path that
   produces the per-thread, per-library, activity-feed state the
   browser needs to render.
2. **No** real keyboard reader — there is no controlling TTY inside
   the container, and ``termios.tcgetattr`` on a non-TTY raises
   ``OSError`` that the engine's outer ``except`` would swallow
   silently. Instead we install a stub that blocks on the engine's
   ``stop_event`` and stashes a reference so the ``/api/job/stop``
   endpoint can flip it.

Both patches are *monkey patches at runtime*, not edits to the engine
source. The engine files themselves remain byte-for-byte unchanged.

This module is imported exactly once, from :mod:`server.app` at
application startup. Importing it more than once is idempotent — the
patch detects whether it has already been applied.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import services.dashboard as _dash
import services.exporter as _exporter
import services.importer as _importer
import services.state as _state


# ── State held across the lifetime of a single engine run ──────────────────────

# The stop_event passed to the keyboard-thread stub by the currently
# running engine call. ``None`` between runs. Set by /api/job/stop to
# request a clean shutdown of the active job.
_active_stop_event: Optional[threading.Event] = None

# A guard so the patch isn't double-applied if the module gets reloaded
# (e.g. by uvicorn --reload during development).
_PATCHED = False


# ── Replacement functions ─────────────────────────────────────────────────────

def _always_full_dashboard() -> bool:
    """
    Replacement for :func:`services.dashboard._check_terminal_size`.

    The original returns ``True`` (= use the fallback Rich Progress
    bars) when stdout isn't a TTY or the terminal is too small. In the
    server we always want the full :class:`DashboardState` code path
    so the WebSocket layer can read structured state, so this just
    returns ``False`` unconditionally.
    """
    return False


def _server_keyboard_stub(log_dir: str, logger: logging.Logger, stop_event: threading.Event) -> None:
    """
    Replacement for :func:`services.dashboard._keyboard_thread`.

    The original opens stdin in raw mode and reads keypresses. Inside
    a container there is no controlling TTY — opening raw mode raises
    ``OSError``. Instead we stash a reference to ``stop_event`` so the
    REST ``/api/job/stop`` endpoint can call ``signal_stop()`` and
    flip the same flag that the [Q] key flips in CLI mode. The engine
    will then drain its pending futures and exit cleanly, exactly as
    the CLI does.

    The stub runs on a daemon thread (the engine still calls it via
    ``threading.Thread(..., daemon=True)``), so it cannot block
    process exit if the server is shut down with a job in flight.
    """
    global _active_stop_event
    _active_stop_event = stop_event
    try:
        # Block here until either the engine signals stop_event itself
        # (e.g. when all libraries finish and run_export returns) or
        # signal_stop() flips it from the API side.
        stop_event.wait()
    finally:
        _active_stop_event = None


def signal_stop() -> bool:
    """
    Request a clean shutdown of the currently running engine job.

    Returns ``True`` if a stop was signalled, ``False`` if no engine
    job is currently running.

    Logs an INFO line to the per-run "plexmigrate" logger so the message
    shows up in runtime.log while the user is tailing it from the web UI.
    The frontend's Stop button surfaces a "Stopping…" state as soon as
    JobQueue.request_stop flips the JobRecord, but this log line gives
    the long-form story: the engine will exit at its next library boundary.
    """
    ev = _active_stop_event
    if ev is None:
        return False
    try:
        logging.getLogger("plexmigrate").info(
            "Stop requested — finishing current library and exiting."
        )
    except Exception:  # pragma: no cover (defensive)
        pass
    ev.set()
    return True


# ── Public entry point ───────────────────────────────────────────────────────

def enable_headless_mode() -> None:
    """
    Apply the runtime patches. Safe to call multiple times.

    Must be called *before* the engine's ``run_export`` or
    ``run_import`` is invoked. :func:`server.app.create_app` calls it
    at module import time so the order is guaranteed.

    Why patch three modules instead of just ``services.dashboard``:
    ``services/exporter.py`` and ``services/importer.py`` both do
    ``from services.dashboard import _check_terminal_size, _keyboard_thread``,
    which copies the function references into their own module
    namespaces at import time. Reassigning the attribute on
    ``services.dashboard`` alone would not reach those copies, so
    ``run_export`` and ``run_import`` would still see the originals
    and fall into the CLI-only Rich Progress fallback path (which is
    not designed for non-TTY hosts). We rebind the same names in
    every importing module so the patch is total regardless of which
    module's call site is on the stack.
    """
    global _PATCHED
    if _PATCHED:
        return
    for mod in (_dash, _exporter, _importer):
        mod._check_terminal_size = _always_full_dashboard
        mod._keyboard_thread = _server_keyboard_stub
    # Tell setup_logging() to skip the sys.excepthook rebind — uvicorn
    # owns that hook in server mode.
    _state.HEADLESS_MODE = True
    _PATCHED = True
