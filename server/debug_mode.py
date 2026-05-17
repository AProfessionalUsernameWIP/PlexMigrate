"""
Developer / debug mode flag (Feature 3 phase 3.1; Tunables Danger-Zone
extension 2026-05-15).

A boolean read from two sources, in order of precedence:

1. ``PLEXMIGRATE_DEBUG_MODE`` env var. Truthy values: ``1``, ``true``,
   ``yes``, ``on`` (case-insensitive, whitespace-trimmed). Anything
   else, or the var being unset, falls through to (2). When set
   truthy the env var ALWAYS WINS and the tunable fallback is
   ignored - this preserves the original "production locks this
   on via a container-launch flag" invariant for any deployment that
   relied on it.

2. ``services.tunables.developer_mode_enabled()`` - a Danger-Zone
   tunable in settings.json. Default False. The tunable is gated
   behind the Tunables panel's Danger Zone, its "I understand"
   checkbox, AND the ``settings.tunables`` root_admin permission, so
   enabling at runtime requires a deliberate three-click decision
   from a root_admin. Toggling takes effect on the next call - the
   tunables module has its own mtime-keyed cache; no backend
   restart required.

When debug mode is active (via either source), the in-app Developer
Tool surface becomes accessible: the Developer tab renders, and
``/api/dev/*`` endpoints respond instead of 403'ing. When false, the
tab is hidden and the endpoints respond 403 with a clear
"set the env var or flip the tunable" hint.

Security posture:

* The env var stays a process-immutable property. Once the container
  is launched with it set truthy, no in-app action can turn debug
  mode OFF for that process; the end user must restart.
* The tunable can be toggled OFF in the UI at any time. A
  newly-OFF tunable takes effect immediately - the next /api/health
  probe reports ``debug_mode: false`` and the frontend Developer
  tab disappears on the next reload.
* A loud warning is logged the FIRST time ``is_enabled()`` returns
  True per process, regardless of source. End users who left
  developer mode on by accident see it in their startup logs.

Read pattern:

* Live read each call. The env-var fast path short-circuits when
  the var is set truthy (zero-cost lookup). The tunable fallback
  rides services.tunables' own mtime-keyed cache so the per-call
  cost is negligible.
"""

from __future__ import annotations

import logging
import os


log = logging.getLogger("plexmigrate.server.debug_mode")


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ENV_VAR = "PLEXMIGRATE_DEBUG_MODE"


# One-shot loud-warning flag. Logs the warning the first time debug
# mode is observed ON for this process, regardless of source. Re-reads
# never re-emit the warning to avoid log spam.
_warning_emitted: bool = False


def _env_enabled() -> bool:
    """Return True iff PLEXMIGRATE_DEBUG_MODE is set to a truthy value."""
    raw = (os.environ.get(_ENV_VAR) or "").strip().lower()
    return raw in _TRUTHY


def _tunable_enabled() -> bool:
    """Return True iff the settings-json tunable is True. Failure to
    load tunables is treated as False so a corrupt settings file
    cannot accidentally unlock the Developer tab."""
    try:
        from services import tunables
        return tunables.developer_mode_enabled()
    except Exception:
        return False


def is_enabled() -> bool:
    """
    Return True when debug / developer mode is active for this
    process. Two sources, env wins; see module docstring for the
    full precedence explanation.

    A loud warning is logged the FIRST time this function returns
    True per process. Subsequent True-returns are silent so a per-
    request check doesn't flood the log.
    """
    global _warning_emitted
    if _env_enabled():
        if not _warning_emitted:
            log.warning(
                "Developer mode is ON via PLEXMIGRATE_DEBUG_MODE=%r. "
                "The Developer tab and /api/dev/* endpoints are exposed. "
                "Do not run with this flag in production.",
                os.environ.get(_ENV_VAR),
            )
            _warning_emitted = True
        return True
    if _tunable_enabled():
        if not _warning_emitted:
            log.warning(
                "Developer mode is ON via the Tunables Danger Zone "
                "(developer_mode_enabled=true in settings.json). The "
                "Developer tab and /api/dev/* endpoints are exposed. "
                "Do not run with this tunable enabled in production."
            )
            _warning_emitted = True
        return True
    return False


def require_enabled() -> None:
    """
    Raise an :class:`HTTPException(403)` when debug mode is OFF.
    Endpoints that should only respond when developer mode is active
    call this at the top of their handler. A 403 (rather than 404)
    surface tells a developer reading logs that they hit a real but
    gated endpoint; a 404 would hide its existence even from
    contributors trying to use it correctly.
    """
    from fastapi import HTTPException
    if not is_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "Developer-mode endpoint. Set PLEXMIGRATE_DEBUG_MODE=1 "
                "in the server environment, OR flip the "
                "developer_mode_enabled tunable in Settings > Tunables > "
                "Danger Zone, to enable. Never enable in production."
            ),
        )


# ── Test seam ───────────────────────────────────────────────────────────────

def _reset_for_tests() -> None:
    """Drop the cached warning flag so the next ``is_enabled`` call
    re-emits the loud warning if it triggers. Test-only; production
    never needs to refresh the flag."""
    global _warning_emitted
    _warning_emitted = False
