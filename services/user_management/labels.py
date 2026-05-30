"""
Render end user-visible labels for Plex owners in runtime UI surfaces.

Supports three configurable styles via ``services.tunables.owner_display_style()``:
  * "plex_owner" - always show "Plex Owner" (default).
  * "custom_name" - substitute the owner's custom display name from the dashboard.
  * "custom_name_owner" - custom name with " (owner)" suffix for unambiguous labeling.
"""

from __future__ import annotations

from typing import Optional

from services import state, tunables


_FALLBACK_LABEL = "Plex Owner"


def _resolve_custom_name(email: str) -> str:
    """Look up email in the live dashboard's cached display-name map."""
    if not email:
        return ""
    try:
        dash = state.get_dashboard()
    except Exception:
        return ""
    if dash is None:
        return ""
    mapping = getattr(dash, "user_display_names", None)
    if not isinstance(mapping, dict):
        return ""
    return str(mapping.get(email) or "").strip()


def owner_display_label(
    *,
    email: Optional[str] = None,
    custom_name: Optional[str] = None,
    style: Optional[str] = None,
) -> str:
    """
    Return the end user-visible label for the Plex owner in the current
    run.

    Args:
        email: Override the owner email used for custom-name lookup.
            Defaults to ``state._plex_owner_email``.
        custom_name: Override the resolved custom name. When provided,
            the dashboard map is not consulted. Useful for tests and
            for call sites that already know the name.
        style: Override the tunable-driven style. Useful for tests.
            Unknown values fall through to "plex_owner".

    Returns:
        A non-empty string suitable for inlining into an activity-feed
        entry or a log line. The function is total: any failure to
        resolve a custom name falls back to ``"Plex Owner"``.
    """
    effective_style = style if style is not None else tunables.owner_display_style()
    if effective_style not in ("plex_owner", "custom_name", "custom_name_owner"):
        effective_style = "plex_owner"

    if effective_style == "plex_owner":
        return _FALLBACK_LABEL

    if custom_name is None:
        try:
            owner_email = email if email is not None else state._plex_owner_email
        except Exception:
            owner_email = ""
        custom_name = _resolve_custom_name(owner_email or "")

    custom_name = (custom_name or "").strip()
    if not custom_name:
        return _FALLBACK_LABEL

    if effective_style == "custom_name":
        return custom_name
    return f"{custom_name} (owner)"
