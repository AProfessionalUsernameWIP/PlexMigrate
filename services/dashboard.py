"""
Dashboard UI, keyboard handling, and progress utilities for PlexMigrate.

Contains DashboardState (the thread-safe state model), _build_dashboard
(the Rich Panel renderer), _keyboard_thread (raw key capture), and helpers
used by both the export and import pipelines.
"""

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.text import Text

import services.state as state
from services.state import VERSION, PLEX_PORT, console


# ── Dashboard Data Classes ────────────────────────────────────────────────────

@dataclass
class ActivityEntry:
    """One entry in the live activity feed (last 8 significant events)."""
    timestamp: str
    action_type: str
    library: str
    title: str


@dataclass
class LibraryProgress:
    """Per-library tracking for progress bars in the dashboard."""
    name: str
    total: int = 0
    completed: int = 0
    status: str = "queued"   # queued | active | done | error
    phase: str = ""
    start_time: float = 0.0


@dataclass
class CurrentItem:
    """
    What one worker thread is processing right now, surfaced on the
    dashboard's "Currently Processing" panel.

    ``phase`` distinguishes *what kind of work* the thread is doing on
    this item — e.g. ``"resolving"`` (looking it up on the target),
    ``"scrobbling"`` (writing the view count), ``"rating"``,
    ``"merging"`` (adding to a playlist/collection), ``"exporting"``
    (reading from source), ``"indexing"`` (scan-cache build),
    ``"fetching"`` (playlist enumeration warmup). The dashboard
    renders this as a column so the user can tell a worker stuck
    mid-resolve apart from one mid-write.
    """
    library: str
    item_type: str
    title: str
    started_at: float
    phase: str = ""


# ── Dashboard State ───────────────────────────────────────────────────────────

class DashboardState:
    """
    Single source of truth for the live terminal dashboard.

    All worker threads write to this object; a dedicated display loop reads
    snapshots from it at 4 Hz and renders the panel via Rich Live.
    Separating state from rendering means workers never block on display
    code and the display never sees a half-updated state.
    """

    def __init__(self, log_dir: str = "") -> None:
        self._lock = threading.Lock()
        self.libraries: Dict[str, LibraryProgress] = {}
        self._lib_order: List[str] = []
        self.activity: Deque[ActivityEntry] = deque(maxlen=8)
        self.completed = 0
        self.skipped = 0
        self.failed = 0
        self.guid_hits = 0
        self.filepath_hits = 0
        self.suffix_hits = 0
        self.fuzzy_hits = 0
        self.unresolved = 0
        self._threads: Dict[int, str] = {}
        # ── Run-coverage counters (new) ──────────────────────────────
        # How much data this run is touching, broken out by category so
        # the dashboard can show "5 users · 12,481 watched · 47 playlists
        # · 314 collections · 89 ratings" at a glance. These count items
        # *enumerated* (export side) or *processed* (import side), not
        # only items that succeeded — keep them in sync with the run's
        # actual scope.
        self.home_user_count = 0    # includes the Plex owner (set via set_user_count)
        self.watch_count = 0
        self.playlist_count = 0
        self.collection_count = 0
        self.rating_count = 0
        # ── Currently-processing items (new) ─────────────────────────
        # Keyed by threading.get_ident() so updates from worker threads
        # don't collide. The dashboard renders one row per active worker.
        self.current_items: Dict[int, CurrentItem] = {}
        self._pause_event = threading.Event()
        self._pause_event.set()
        self.paused = False
        self.start_time = time.time()
        self.log_dir = log_dir

    def add_library(self, name: str, total: int) -> None:
        with self._lock:
            if name not in self.libraries:
                self.libraries[name] = LibraryProgress(name=name, total=total)
                self._lib_order.append(name)

    def set_library_status(self, name: str, status: str) -> None:
        with self._lock:
            if name in self.libraries:
                lib = self.libraries[name]
                lib.status = status
                if status == "active" and lib.start_time == 0.0:
                    lib.start_time = time.time()

    def set_library_phase(self, name: str, phase: str) -> None:
        with self._lock:
            if name in self.libraries:
                self.libraries[name].phase = phase

    def advance_library(self, name: str, n: int = 1) -> None:
        with self._lock:
            if name in self.libraries:
                lib = self.libraries[name]
                lib.completed = min(lib.completed + n, lib.total)

    def finish_library(self, name: str, error: bool = False) -> None:
        with self._lock:
            if name in self.libraries:
                lib = self.libraries[name]
                lib.status = "error" if error else "done"
                lib.completed = lib.total
                lib.phase = "Error" if error else "Done"

    def inc_completed(self) -> None:
        with self._lock:
            self.completed += 1

    def inc_skipped(self) -> None:
        with self._lock:
            self.skipped += 1

    def inc_failed(self) -> None:
        with self._lock:
            self.failed += 1

    def inc_guid(self) -> None:
        with self._lock:
            self.guid_hits += 1

    def inc_filepath(self) -> None:
        with self._lock:
            self.filepath_hits += 1

    def inc_suffix(self) -> None:
        with self._lock:
            self.suffix_hits += 1

    def inc_fuzzy(self) -> None:
        with self._lock:
            self.fuzzy_hits += 1

    def inc_unresolved(self) -> None:
        with self._lock:
            self.unresolved += 1

    # ── Run-coverage counters ────────────────────────────────────────

    def set_user_count(self, n: int) -> None:
        """Record total users covered by this run (includes the owner)."""
        with self._lock:
            self.home_user_count = max(0, int(n))

    def inc_watch(self, n: int = 1) -> None:
        with self._lock:
            self.watch_count += n

    def inc_playlist(self, n: int = 1) -> None:
        with self._lock:
            self.playlist_count += n

    def inc_collection(self, n: int = 1) -> None:
        with self._lock:
            self.collection_count += n

    def inc_rating(self, n: int = 1) -> None:
        with self._lock:
            self.rating_count += n

    # ── Currently-processing items ───────────────────────────────────

    def set_current_item(
        self,
        library: str,
        item_type: str,
        title: str,
        phase: str = "",
    ) -> None:
        """
        Mark the calling thread as actively processing one item.

        Callers can call this multiple times to advance the phase
        (e.g. ``"resolving"`` → ``"scrobbling"``) without bumping
        ``started_at`` — the latter is preserved so the Age column on
        the dashboard reflects total time spent on the item, not just
        on the current phase.
        """
        tid = threading.get_ident()
        with self._lock:
            existing = self.current_items.get(tid)
            started = existing.started_at if existing else time.time()
            self.current_items[tid] = CurrentItem(
                library=library,
                item_type=item_type,
                title=title,
                started_at=started,
                phase=phase,
            )

    def clear_current_item(self) -> None:
        with self._lock:
            self.current_items.pop(threading.get_ident(), None)

    def push_activity(self, action_type: str, library: str, title: str) -> None:
        with self._lock:
            self.activity.append(ActivityEntry(
                timestamp=datetime.now().strftime("%H:%M:%S"),
                action_type=action_type,
                library=library,
                title=title,
            ))

    def register_thread(self, category: str) -> None:
        with self._lock:
            self._threads[threading.get_ident()] = category

    def unregister_thread(self) -> None:
        with self._lock:
            self._threads.pop(threading.get_ident(), None)

    def toggle_pause(self) -> None:
        with self._lock:
            if self._pause_event.is_set():
                self._pause_event.clear()
                self.paused = True
            else:
                self._pause_event.set()
                self.paused = False

    def wait_if_paused(self) -> None:
        """Block the calling thread when paused. Returns immediately when running."""
        self._pause_event.wait()

    def snapshot(self) -> Dict[str, Any]:
        """Returns a JSON-like dict copy of the current state for rendering."""
        now = time.time()
        with self._lock:
            return {
                "libraries": [
                    {
                        "name": lib.name,
                        "total": lib.total,
                        "completed": lib.completed,
                        "status": lib.status,
                        "phase": lib.phase,
                        "start_time": lib.start_time,
                    }
                    for lib in (self.libraries[n] for n in self._lib_order)
                ],
                "activity": [
                    {
                        "timestamp": e.timestamp,
                        "action_type": e.action_type,
                        "library": e.library,
                        "title": e.title,
                    }
                    for e in self.activity
                ],
                "completed": self.completed,
                "skipped": self.skipped,
                "failed": self.failed,
                "guid_hits": self.guid_hits,
                "filepath_hits": self.filepath_hits,
                "suffix_hits": self.suffix_hits,
                "fuzzy_hits": self.fuzzy_hits,
                "unresolved": self.unresolved,
                # ── Run-coverage (new) ───────────────────────────────
                "home_user_count": self.home_user_count,
                "watch_count": self.watch_count,
                "playlist_count": self.playlist_count,
                "collection_count": self.collection_count,
                "rating_count": self.rating_count,
                # ── Currently-processing (new) ───────────────────────
                "current_items": [
                    {
                        "library": ci.library,
                        "type": ci.item_type,
                        "title": ci.title,
                        "started_at": ci.started_at,
                        "phase": ci.phase,
                    }
                    for ci in self.current_items.values()
                ],
                "threads": dict(self._threads),
                "paused": self.paused,
                "start_time": self.start_time,
                "log_dir": self.log_dir,
                "now": now,
            }


# ── Thread Category Context Manager ──────────────────────────────────────────

@contextlib.contextmanager
def _thread_category(category: str):
    """
    Registers the calling thread's work category in the dashboard.

    The Thread Pool panel shows how many threads are in each category
    (Play Count, Ratings, Scan Cache, etc.). Wrapping worker work with
    this context manager keeps the tracking out of worker function bodies.
    """
    if state._dashboard:
        state._dashboard.register_thread(category)
    try:
        yield
    finally:
        if state._dashboard:
            state._dashboard.unregister_thread()


@contextlib.contextmanager
def _current_item(library: str, item_type: str, title: str, phase: str = ""):
    """
    Mark the calling thread as actively processing one item.

    Wrap the per-item work (resolve + write) so the dashboard's
    "Currently Processing" panel can show what each worker is doing
    right now. Cheap (one lock acquire on entry, one on exit); the
    dashboard reads at 4 Hz so transient items still appear.

    ``phase`` is a short verb describing the kind of work in flight
    — see :class:`CurrentItem` for the canonical strings.

    No-op when no dashboard is attached (CLI fallback / tests).
    """
    if state._dashboard:
        state._dashboard.set_current_item(library, item_type, title, phase)
    try:
        yield
    finally:
        if state._dashboard:
            state._dashboard.clear_current_item()


# ── Dashboard Utilities ───────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    """Formats elapsed seconds as M:SS or H:MM:SS (e.g. '3:07' or '1:02:45')."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sc = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sc:02d}"
    return f"{m}:{sc:02d}"


def _check_terminal_size() -> bool:
    """
    Returns True if the terminal is too small for the full dashboard.

    The threshold is 80 columns × 22 rows — below that we fall back to the
    simple Rich Progress bars used in v0.4.0. Also returns True when stdout
    is not a TTY (piped output), since Live mode doesn't make sense there.
    """
    if not sys.stdout.isatty():
        return True
    try:
        cols, rows = os.get_terminal_size()
        return cols < 80 or rows < 22
    except OSError:
        return True


def _open_log_folder(log_dir: str) -> None:
    """Opens the log directory in the OS file manager (best-effort, silent on failure)."""
    try:
        target = os.path.abspath(log_dir)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception:
        pass


def _open_plex_server() -> None:
    """Opens the connected Plex server in the default browser, auto-logged in via token."""
    try:
        base = (state._plex_base_url or f"http://localhost:{PLEX_PORT}").rstrip("/")
        if state._plex_token:
            url = f"{base}/web/index.html?X-Plex-Token={state._plex_token}"
        else:
            url = base
        webbrowser.open(url)
    except Exception:
        pass


def _make_progress() -> Progress:
    """
    Builds the Rich Progress instance used for all export and import bars.

    Layout per row:
        [spinner] [library name, 25 chars] [bar, 22 wide] [N/M] [phase label, 22 chars] [ETA]
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description:<25}"),
        BarColumn(bar_width=22),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[phase]:<22}"),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=8,
    )


# ── Dashboard Rendering ───────────────────────────────────────────────────────

_ACTION_COLORS: Dict[str, str] = {
    "merged":      "green",
    "created":     "green",
    "appended":    "cyan",
    "skipped":     "dim",
    "rating_set":  "yellow",
    "failed":      "red",
    "unresolved":  "bold red",
    "phase":       "blue",
    "started":     "white",
    "done":        "bold green",
    "error":       "bold red",
}

_ACTION_LABELS: Dict[str, str] = {
    "merged":      "MERGED",
    "created":     "CREATED",
    "appended":    "APPENDED",
    "skipped":     "SKIPPED",
    "rating_set":  "RATED",
    "failed":      "FAILED",
    "unresolved":  "UNRESOLVED",
    "phase":       "PHASE",
    "started":     "STARTED",
    "done":        "DONE",
    "error":       "ERROR",
}

_THREAD_CATEGORIES: Dict[str, str] = {
    "watched":       "Watched",
    "play_count":    "Play Count",
    "playlists":     "Playlists",
    "collections":   "Collections",
    "ratings":       "Ratings",
    "scan_cache":    "Scan Cache",
    "home_user":     "Home User",
    "export":        "Exporting",
}


def _mini_bar(completed: int, total: int, width: int = 20) -> str:
    """Returns a fixed-width ASCII progress bar string: '████░░░░░░░░░░░░░░░░'."""
    if total <= 0:
        return "░" * width
    filled = int(min(1.0, completed / total) * width)
    return "█" * filled + "░" * (width - filled)


def _build_dashboard(snap: Dict[str, Any], mode: str = "IMPORT") -> Panel:
    """
    Renders the full terminal dashboard from a DashboardState snapshot.

    Returns a Rich Panel containing structured Text. The Panel is passed to
    Live.update() on each display refresh. All column widths are fixed so the
    panel does not shift between frames.
    """
    now_str = datetime.now().strftime("%H:%M:%S")
    elapsed = snap["now"] - snap["start_time"]
    elapsed_str = _fmt_duration(elapsed)
    paused_tag = "  [PAUSED]" if snap["paused"] else ""

    body = Text()

    # ── Header ─────────────────────────────────────────────────────────────────
    body.append(
        f" PlexMigrate v{VERSION}  ·  {mode}  ·  {now_str}  ·  Elapsed {elapsed_str}{paused_tag}\n",
        style="bold",
    )

    # ── Thread Pool summary ────────────────────────────────────────────────────
    cats: Dict[str, int] = {}
    for cat in snap["threads"].values():
        label = _THREAD_CATEGORIES.get(cat, cat)
        cats[label] = cats.get(label, 0) + 1
    thread_str = "  ".join(f"{lbl} ×{n}" for lbl, n in sorted(cats.items())) if cats else "—"
    body.append(" THREADS   ", style="bold dim")
    body.append(thread_str + "\n", style="dim")

    # ── Run stats ──────────────────────────────────────────────────────────────
    body.append(" STATS     ", style="bold dim")
    body.append(
        f"Completed: {snap['completed']:,}  Skipped: {snap['skipped']:,}  "
        f"Failed: {snap['failed']:,}  Unresolved: {snap['unresolved']:,}\n",
        style="dim",
    )

    # ── Match resolution breakdown ─────────────────────────────────────────────
    body.append(" MATCH     ", style="bold dim")
    body.append(
        f"GUID: {snap['guid_hits']:,}  Filepath: {snap['filepath_hits']:,}  "
        f"Suffix: {snap['suffix_hits']:,}  Fuzzy: {snap['fuzzy_hits']:,}\n",
        style="dim",
    )

    # ── Per-library progress bars ──────────────────────────────────────────────
    body.append("─" * 74 + "\n", style="dim")
    body.append(
        f" {'Library':<14}  {'Progress':<20}  {'Items':<10}  {'Phase':<16}  ETA\n",
        style="bold dim",
    )
    for lib in snap["libraries"]:
        name = lib["name"][:14].ljust(14)
        total = lib["total"] or 1
        comp = lib["completed"]
        bar = _mini_bar(comp, total, width=20)
        phase = lib["phase"][:16].ljust(16)
        items_str = f"{comp}/{total} items"

        status = lib["status"]
        if status == "active" and lib["start_time"] > 0:
            elapsed_lib = snap["now"] - lib["start_time"]
            pct = comp / total
            if pct > 0.02 and elapsed_lib > 0:
                eta = elapsed_lib / pct * (1 - pct)
                eta_str = f"ETA {_fmt_duration(eta)}"
            else:
                eta_str = "starting..."
        elif status == "done":
            eta_str = "Done      "
        elif status == "error":
            eta_str = "Error     "
        else:
            eta_str = "Queued    "

        status_style = {
            "done": "green", "error": "red", "active": "white", "queued": "dim",
        }.get(status, "white")
        body.append(
            f" {name}  {bar}  {items_str:<10}  {phase}  {eta_str}\n",
            style=status_style,
        )

    # ── Activity feed ──────────────────────────────────────────────────────────
    body.append("─" * 74 + "\n", style="dim")
    feed = snap["activity"][-4:] if len(snap["activity"]) > 4 else snap["activity"]
    if feed:
        for entry in feed:
            color = _ACTION_COLORS.get(entry["action_type"], "white")
            label = _ACTION_LABELS.get(entry["action_type"], entry["action_type"].upper()).ljust(12)
            lib_short = entry["library"][:10].ljust(10)
            title = entry["title"][:36]
            row = Text()
            row.append(f" {entry['timestamp']}  ", style="dim")
            row.append(label, style=color)
            row.append(f"  {lib_short}  {title}\n")
            body.append_text(row)
    else:
        body.append(" (no activity yet)\n", style="dim")

    # ── Keys strip ─────────────────────────────────────────────────────────────
    body.append("─" * 74 + "\n", style="dim")
    body.append(
        " [Q] Quit   [V] Verbose   [P] Pause/Resume   [L] Open Logs   [S] Open Server   [R] Refresh",
        style="bold dim",
    )

    return Panel(body, border_style="dim", padding=(0, 0))


# ── Keyboard Input Handling ───────────────────────────────────────────────────

def _handle_key(
    key: str,
    log_dir: str,
    logger: Any,
    stop_event: threading.Event,
) -> None:
    """
    Dispatches a single keypress to the appropriate action.

    Key bindings:
        Q — cancel queued work and exit cleanly after running tasks finish
        V — toggle RichHandler console log level between INFO and DEBUG
        P — pause/resume all worker threads at their next checkpoint
        L — open the log directory in the OS file manager
        S — open the connected Plex server in the default web browser
        R — force an immediate dashboard refresh
    """
    k = key.lower()
    if k == "q":
        stop_event.set()
        if state._dashboard:
            state._dashboard.push_activity("phase", "—", "Stopping (finishing current tasks)…")
    elif k == "v":
        if state._console_handler is not None:
            if state._console_handler.level == logging.DEBUG:
                state._console_handler.setLevel(logging.INFO)
                logger.info("Verbose console logging disabled")
            else:
                state._console_handler.setLevel(logging.DEBUG)
                logger.info("Verbose console logging enabled")
    elif k == "p":
        if state._dashboard is not None:
            state._dashboard.toggle_pause()
    elif k == "l":
        _open_log_folder(log_dir)
    elif k == "s":
        _open_plex_server()
    elif k == "r":
        if state._live_instance is not None:
            state._live_instance.refresh()


def _keyboard_thread(
    log_dir: str,
    logger: Any,
    stop_event: threading.Event,
) -> None:
    """
    Background daemon thread that reads keyboard input without blocking the main thread.

    Windows path:
        Uses msvcrt.kbhit() to check for input and msvcrt.getwch() to read one
        wide character without echoing it to the terminal.

    Unix/macOS path:
        Sets the terminal to raw mode (tty.setraw) so characters arrive without
        waiting for Enter, then uses select() with a 50 ms timeout to avoid
        busy-waiting. The original terminal settings are restored in the finally
        block even if the thread is killed by an exception.
    """
    try:
        if sys.platform == "win32":
            import msvcrt
            while not stop_event.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    _handle_key(ch, log_dir, logger, stop_event)
                time.sleep(0.05)
        else:
            import tty
            import termios
            import select as _select
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while not stop_event.is_set():
                    r, _, _ = _select.select([sys.stdin], [], [], 0.05)
                    if r:
                        ch = sys.stdin.read(1)
                        _handle_key(ch, log_dir, logger, stop_event)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception:
        pass


# ── Action Type and Progress Helpers ─────────────────────────────────────────

def _action_type_from_record(record: Dict) -> str:
    """
    Maps a success record's action string to a dashboard activity action_type.

    _record_success() is called from many places with different action strings.
    This helper infers the action_type from the already-present action string
    so no existing call sites need to change.
    """
    action = record.get("action", "")
    if "[CREATED]" in action:
        return "created"
    if "[APPENDED]" in action:
        return "appended"
    if "[SKIPPED" in action or "skipped" in action.lower():
        return "skipped"
    if "[RATING SET]" in action:
        return "rating_set"
    return "merged"


def _advance_lib(lib_name: str) -> None:
    """
    Advances the progress display for lib_name by one step.

    Works in both dashboard mode (_dashboard) and small-terminal fallback mode
    (_live_progress), so import functions only need to call this once instead of
    duplicating the if/elif logic at every progress-advance site.
    """
    if state._dashboard:
        state._dashboard.advance_library(lib_name)
    elif state._live_progress:
        state._live_progress.update(state._lib_task_ids.get(lib_name), advance=1)
