"""
End user-visible labels for Plex users in runtime UI surfaces.

Today this module is used by the snapshot engine's activity-feed
emissions to render the Plex owner with a configurable style:

* "plex_owner" (default) - always show the literal "Plex Owner".
* "custom_name" - substitute the end user-configured display name
  for the owner's Plex.tv email when one exists on the source
  server's ``user_display_names`` map. Falls back to "Plex Owner".
* "custom_name_owner" - as above but appends " (owner)" so the
  owner is unambiguous when a managed user happens to share a
  similar custom name.

The style is read from :func:`services.tunables.owner_display_style`.
Display names come from the live dashboard's cached map, which
:func:`server.jobs._populate_run_user_context` populates at run start.

Engine logs, export files, and database rows continue to use raw
Plex identifiers regardless of this style; this module is purely a
rendering aid.
"""

from __future__ import annotations

from typing import Optional

from services import state, tunables


_FALLBACK_LABEL = "Plex Owner"


def _resolve_custom_name(email: str) -> str:
    """
    Look up ``email`` in the live dashboard's cached display-name map.
    Returns an empty string when no dashboard is attached, no map is
    cached, or no entry exists for the email. Never raises.
    """
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
