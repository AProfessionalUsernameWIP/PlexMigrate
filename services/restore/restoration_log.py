"""Per-run restoration log writer emitting one line per (item, user, metric) outcome."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# restoration.log is written by this module DIRECTLY, not through the
# logging framework, so the handler-level TokenScrubFilter never sees
# these lines. Scrub here instead so a credential embedded in a
# reason / detail / title (e.g. a plexapi exception whose __str__
# captured a tokenised request URL) never lands in restoration.log.
# Best-effort: if the scrubber module is somehow unavailable, fall
# back to the identity function so logging still works.
try:
    from server.log_scrubber import scrub as _token_scrub
except Exception:  # pragma: no cover - scrubber is defence-in-depth
    def _token_scrub(text: str) -> str:  # type: ignore[misc]
        return text


STATUS_RESTORED = "RESTORED"
STATUS_NOOP = "NOOP"
STATUS_SKIPPED = "SKIPPED"
STATUS_FAILED = "FAILED"

_ALL_STATUSES: Tuple[str, ...] = (
    STATUS_RESTORED, STATUS_NOOP, STATUS_SKIPPED, STATUS_FAILED,
)

_STATUS_WIDTH = 8
_METRIC_WIDTH = 16


_log = logging.getLogger("plexmigrate.services.restore.restoration_log")


def _format_value(v: Any) -> str:
    """Render a value safely for a single-line key=value emission."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).replace("\n", " ").replace("\r", "")


def _quote_label(v: Any) -> str:
    """Quote item titles + playlist names so spaces are unambiguous."""
    s = "" if v is None else str(v)
    s = s.replace("\n", " ").replace("\r", "")
    return f"'{s}'"


class _NullWriter:
    """
    No-op writer used when the run log dir is empty (run logging
    disabled) or a writer creation failed. Every method is a silent
    no-op so callers don't have to None-check before every emission.
    """
    path: Optional[Path] = None

    def restored(self, **kwargs: Any) -> None: pass
    def noop(self, **kwargs: Any) -> None: pass
    def skipped(self, **kwargs: Any) -> None: pass
    def failed(self, **kwargs: Any) -> None: pass
    def affected_user_list(self) -> List[str]: return []
    def close_with_summary(self) -> None: pass


class RestorationLogWriter:
    """
    Append-only writer for ``restoration.log``. Thread-safe under the
    restorer's worker-pool fan-out: ``_lock`` serialises every line
    emission and every counter bump.

    Each public method appends one line and updates the in-memory
    counters used to render the trailing summary block. Methods accept
    only keyword arguments so future field additions stay backwards
    compatible.
    """

    def __init__(
        self,
        *,
        path: Path,
        dest_server_id: Optional[str] = None,
    ) -> None:
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._closed = False
        # When supplied, every user reference written or aggregated goes through
        # services.identity.user_display.display_for_logging(dest_server_id, user).
        # The helper substitutes managed_users.display_name for the raw
        # username when the log_use_display_name tunable is on, and
        # otherwise returns the raw username unchanged. None preserves
        # the legacy behaviour for callers that haven't been updated
        # to thread the server id through.
        self._dest_server_id: Optional[str] = dest_server_id

        self._total_per_status: Dict[str, int] = defaultdict(int)
        self._per_metric: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._per_library: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._per_user: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )

    def _user_for_log(self, user: str) -> str:
        """Apply the log_use_display_name substitution when a
        dest_server_id was provided at construction. Best-effort:
        on any failure (helper missing, DB closed) returns the raw
        handle so logs always have a value."""
        if not self._dest_server_id:
            return user
        try:
            from services.identity.user_display import display_for_logging
            return display_for_logging(self._dest_server_id, user)
        except Exception:
            return user

    def restored(
        self,
        *,
        library: str,
        item_title: str,
        user: str,
        metric: str,
        before: Any,
        after: Any,
        duration_ms: int,
    ) -> None:
        """
        Engine wrote a change to the destination. ``before`` and
        ``after`` are the destination's state pre- and post-write.
        """
        self._emit(
            STATUS_RESTORED, library, user, metric,
            label_key="item",
            label_value=item_title,
            extras=(
                ("before", _format_value(before)),
                ("after", _format_value(after)),
                ("duration_ms", str(duration_ms)),
            ),
        )

    def noop(
        self,
        *,
        library: str,
        item_title: str,
        user: str,
        metric: str,
        before: Any = None,
        after: Any = None,
        reason: str = "already_matched",
    ) -> None:
        """
        Engine took no action because the destination already matched
        the snapshot. The default ``reason`` is ``already_matched``;
        callers pass a more specific reason when one applies (e.g.
        ``already_present`` for playlist membership).
        """
        extras = []
        if before is not None or after is not None:
            extras.append(("before", _format_value(before)))
            extras.append(("after", _format_value(after)))
        extras.append(("reason", _format_value(reason)))
        self._emit(
            STATUS_NOOP, library, user, metric,
            label_key="item",
            label_value=item_title,
            extras=tuple(extras),
        )

    def skipped(
        self,
        *,
        library: str,
        item_title: str,
        user: str,
        metric: str,
        reason: str,
    ) -> None:
        """
        Engine deliberately did not attempt the write. The canonical
        reason is ``smart_playlist`` (we never re-create smart
        playlists at the destination), but the writer accepts any
        reason string so future skips have a single channel.
        """
        self._emit(
            STATUS_SKIPPED, library, user, metric,
            label_key="item",
            label_value=item_title,
            extras=(
                ("reason", _format_value(reason)),
            ),
        )

    def failed(
        self,
        *,
        library: str,
        item_title: str,
        user: str,
        metric: str,
        reason: str,
    ) -> None:
        """
        Engine attempted the write and it raised (or a precondition
        like resolver-match failed). ``reason`` names the failure
        mode so the end user can fix it.
        """
        self._emit(
            STATUS_FAILED, library, user, metric,
            label_key="item",
            label_value=item_title,
            extras=(
                ("reason", _format_value(reason)),
            ),
        )

    def affected_user_list(self) -> List[str]:
        """
        Return the sorted list of users with at least one RESTORED
        entry in this writer's log. Phase 4 reads this in
        ``server.jobs`` finalization to populate the
        ``run_history.users_affected_list`` column.

        Per the end user's spec ("users_affected counts only users who
        actually had to have things restored"), a user whose every
        entry is NOOP / SKIPPED / FAILED is not affected.
        """
        with self._lock:
            return sorted(
                u for u, counts in self._per_user.items()
                if counts.get(STATUS_RESTORED, 0) > 0
            )

    def close_with_summary(self) -> None:
        """
        Write the trailing summary block and close the file. Idempotent:
        a second call is a no-op so the engine's finally-block can
        always call it without worrying about earlier paths.
        """
        with self._lock:
            if self._closed:
                return
            try:
                wall = time.time() - self._started_at
                lines = self._render_summary(wall_seconds=wall)
                self._fh.write("\n")
                self._fh.write("\n".join(lines))
                self._fh.write("\n")
                self._fh.flush()
            except Exception:
                _log.exception("restoration_log: failed writing summary block")
            try:
                self._fh.close()
            except Exception:
                pass
            self._closed = True

    def _emit(
        self,
        status: str,
        library: str,
        user: str,
        metric: str,
        *,
        label_key: str,
        label_value: str,
        extras: Tuple[Tuple[str, str], ...],
    ) -> None:
        if self._closed:
            return
        try:
            # Apply log_use_display_name substitution (when enabled +
            # dest_server_id was wired through) BEFORE writing the
            # line + aggregating per-user counters. Both the line and
            # the summary's per-user breakdown read the substituted
            # value so the end user sees a consistent name across
            # both surfaces.
            user_for_log = self._user_for_log(user)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            parts = [
                f"[{ts}]",
                status.ljust(_STATUS_WIDTH),
                str(metric).ljust(_METRIC_WIDTH),
                f"lib={library}",
                f"{label_key}={_quote_label(label_value)}",
                f"user={user_for_log}",
            ]
            for k, v in extras:
                parts.append(f"{k}={v}")
            # Scrub any credential (X-Plex-Token, Fernet blob, ...)
            # before the line touches disk. reason= strings routinely
            # embed a raw exception message, and this writer bypasses
            # the logging framework so the handler-level scrubber
            # never gets a chance to redact it.
            line = _token_scrub(" ".join(parts)) + "\n"

            with self._lock:
                if self._closed:
                    return
                self._fh.write(line)
                self._fh.flush()
                self._total_per_status[status] += 1
                self._per_metric[metric][status] += 1
                self._per_library[library][status] += 1
                self._per_user[user_for_log][status] += 1
        except Exception:
            # Never let a logging failure abort a restore.
            # The dashboard counters track real progress; this log is parallel.
            _log.exception("restoration_log: emit failed (%s)", status)

    def _render_summary(self, *, wall_seconds: float) -> list:
        lines = [
            "======================================",
            "Restoration summary",
            "======================================",
        ]

        total_entries = sum(self._total_per_status.values())
        lines.append(f"Total entries:     {total_entries}")
        for status in _ALL_STATUSES:
            lines.append(
                f"  {status}:".ljust(20)
                + str(self._total_per_status.get(status, 0))
            )

        if self._per_metric:
            lines.append("By metric:")
            for metric in sorted(self._per_metric):
                counts = self._per_metric[metric]
                lines.append(
                    f"  {metric}:".ljust(20)
                    + ", ".join(
                        f"{counts.get(s, 0)} {s}" for s in _ALL_STATUSES
                    )
                )

        if self._per_library:
            lines.append("By library:")
            for lib in sorted(self._per_library):
                counts = self._per_library[lib]
                restored = counts.get(STATUS_RESTORED, 0)
                failed = counts.get(STATUS_FAILED, 0)
                suffix = f" ({failed} failure{'s' if failed != 1 else ''})" if failed else ""
                lines.append(
                    f"  {lib}:".ljust(20)
                    + f"{restored} change{'s' if restored != 1 else ''}{suffix}"
                )

        if self._per_user:
            lines.append("By user:")
            for user in sorted(self._per_user):
                counts = self._per_user[user]
                restored = counts.get(STATUS_RESTORED, 0)
                lines.append(
                    f"  {user}:".ljust(20)
                    + f"{restored} change{'s' if restored != 1 else ''}"
                )

        lines.append(f"Total wall-clock:  {wall_seconds:.1f}s")
        return lines


def open_restoration_log(
    run_log_dir: str,
    *,
    logger: Optional[logging.Logger] = None,
    dest_server_id: Optional[str] = None,
) -> RestorationLogWriter:
    """
    Open the per-run ``restoration.log`` writer or return a
    :class:`_NullWriter` shim when run logging is disabled / the path
    cannot be opened. The returned object is safe to call ``.restored``
    / ``.noop`` / ``.skipped`` / ``.failed`` / ``.close_with_summary``
    on regardless of whether a real file is attached.

    The caller normally invokes ``close_with_summary()`` in a finally
    block so the summary block is always written even on early exits.

    ``dest_server_id`` enables the log_use_display_name cosmetic
    substitution. When supplied, every ``user=`` token written by the
    writer (per-row lines + per-user summary block) goes through
    ``services.identity.user_display.display_for_logging(dest_server_id, user)``;
    callers that don't supply the id get the legacy raw-username
    behaviour.
    """
    if not run_log_dir:
        if logger is not None:
            try:
                logger.info(
                    "restoration_log: run logging disabled - no restoration.log will be written"
                )
            except Exception:
                pass
        return _NullWriter()  # type: ignore[return-value]
    try:
        path = Path(run_log_dir) / "restoration.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = RestorationLogWriter(path=path, dest_server_id=dest_server_id)
        if logger is not None:
            try:
                logger.info(
                    "RESTORATION LOG: per-item restore outcomes written to %s",
                    path,
                )
            except Exception:
                pass
        return writer
    except Exception as exc:
        if logger is not None:
            try:
                logger.warning(
                    "restoration_log: could not open %s/restoration.log (%s); "
                    "restore continues without per-item log",
                    run_log_dir, exc,
                )
            except Exception:
                pass
        return _NullWriter()  # type: ignore[return-value]