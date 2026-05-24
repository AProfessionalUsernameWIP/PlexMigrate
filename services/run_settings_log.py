"""
Per-run settings snapshot writer.

When a job starts, this module dumps the full configuration that was in
effect, into a human-readable ``run-settings.log`` file alongside the
run log. The end user inspects this file (manually, or via the log
browser) to verify what settings produced a given run, and to spot
drift between job runs that should have been identical.

The writer is best-effort and diagnostic only. Any failure logs a
warning and returns ``None`` rather than raising. The engine never
reads these files; they exist so a human reviewer can answer "what
settings were active when this run happened?" without having to
correlate settings.json mtimes against run timestamps.

Format: Markdown with tables and grouped bullet sections. The file
extension is ``.log`` rather than ``.md`` because it lives in the
per-run log directory and the log-browser is the end user's primary
reader. Markdown renders cleanly as plain text and stays grep-friendly.

Layout:

* Top matter: captured timestamp, job type, trigger.
* ``## At a glance`` - a 6-8 line summary answering the end user's
  first question ("what did this run do?"): source, destinations,
  libraries, output dir, workers, key flags.
* ``## Per-library metrics`` - the per-library_metrics map (if any)
  rendered as a real checkbox-style table, one row per library.
* ``## Tunables`` - overridden tunables first (highlighted). If
  nothing is overridden, a single summary line replaces the full
  28-row dump. The complete default table follows under a collapsed
  "Defaults in effect" header so the data is still available.
* ``## Persistent Settings`` - semantically grouped (Engine workers,
  Paths, Restore defaults, Retention, Auth, Logging, Validation,
  Run defaults, UI, Per-server overrides, Misc). Nested dicts
  (library_walk, media_db_retention, transfer_resolution, etc.)
  are expanded into bullet lines instead of dense JSON.
* ``## Job Params`` - only fields with a non-null, non-empty value
  for the active job_type; null / empty placeholders for other
  modes are hidden behind a one-line footer.
* Footer: a single line clarifying the file is diagnostic.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Field names that, regardless of where they appear, contain credentials
# or other secrets that must never land in a diagnostic dump.
_SECRET_FIELD_NAMES = frozenset((
    "plex_token",
    "token",
    "password",
    "secret",
    "encryption_key",
    "fernet_key",
    "_encrypted",
))


# Persistent-settings semantic groups. Each entry is (group_label,
# [keys_in_order]). Keys not listed here fall into the "Other" bucket
# so a freshly-added setting never disappears, just lands ungrouped.
_PERSISTENT_GROUPS: List[Tuple[str, Tuple[str, ...]]] = [
    ("Engine workers", (
        "workers",
        "scrobble_workers",
        "snapshot_library_workers",
        "restore_library_workers",
        "fan_out_destination_workers",
        "smart_bulk_threshold_items",
    )),
    ("Paths", (
        "output_dir",
        "log_dir",
    )),
    ("Restore defaults", (
        "restore_defaults",
        "strict_match",
    )),
    ("Run defaults", (
        "prebuild_json_sidecar_default",
        "watch_ratings_filter_strategy",
        "verbose",
    )),
    ("Retention", (
        "snapshot_retention_global",
        "snapshot_retention_per_server",
        "snapshot_defaults_per_server",
        "run_timings_retention_count",
    )),
    ("Logging", (
        "run_logging_enabled",
        "log_rotate_backup_count",
        "log_rotate_max_size_mb",
    )),
    ("Validation", (
        "validate_snapshot_after_capture",
        "validate_snapshot_before_restore",
    )),
    ("Auth", (
        "audit_log_enabled",
        "elevation_ttl_seconds",
        "auto_rotate_tokens_on_refresh",
        "pin_migration_allow_username_fallback",
        "user_token_capture_throttle_per_hour",
    )),
    ("Library walk", (
        "library_walk",
    )),
    ("Media DB retention", (
        "media_db_retention",
    )),
    ("Transfer resolution", (
        "transfer_resolution",
    )),
    ("Per-server overrides", (
        "tunables_per_server",
    )),
    ("UI", (
        "tooltips_enabled",
        "etr_color_multiplier",
    )),
]

# Keys that should not appear under Persistent Settings at all. The
# ``_encrypted`` marker is rendered in the footer; ``plex_token`` and
# ``plex_url`` are server-scoped now and the dedicated server registry
# is the right place to look (the dump used to surface a legacy global
# pair that is empty in current installs).
_PERSISTENT_SUPPRESS = frozenset((
    "_encrypted",
    "plex_token",
    "plex_url",
))


# Per-job-type "applicable" allow-list. Keys outside the set for the
# current job type are hidden from the Job Params section because they
# only matter to other modes and add noise. The set is a guideline
# (any key with a non-default value still renders, see _is_blank).
_JOB_PARAM_APPLICABLE: Dict[str, frozenset] = {
    "snapshot": frozenset((
        "_trigger", "source_server_name", "resolved_server_slug",
        "libraries", "library_metrics", "output_dir", "workers",
        "scrobble_workers", "log_dir", "verbose",
        "fast_collection_detection", "skip_playlist_prebuild",
        "prebuild_json_sidecar", "watch_ratings_filter_strategy",
        "user_filter", "include_watch_history", "include_ratings",
        "include_playlists", "include_collections",
        "plex_url",
    )),
    "restore": frozenset((
        "_trigger", "dest_server_names", "dest_server_name",
        "snapshot_id", "input_files", "mode", "merge_watch_strategy",
        "auto_capture_before_replace", "confirm_replace",
        "overwrite_playlists", "remap_old", "remap_new",
        "library_metrics", "user_filter", "workers", "scrobble_workers",
        "log_dir", "verbose", "strict_match",
        "include_watch_history", "include_ratings",
        "include_playlists", "include_collections",
    )),
    "direct": frozenset((
        "_trigger", "source_server_name", "dest_server_names",
        "resolved_server_slug", "libraries", "library_metrics",
        "mode", "merge_watch_strategy", "auto_capture_before_replace",
        "confirm_replace", "remap_old", "remap_new", "strict_match",
        "fast_collection_detection", "user_filter", "workers",
        "scrobble_workers", "log_dir", "verbose",
        "watch_ratings_filter_strategy",
        "include_watch_history", "include_ratings",
        "include_playlists", "include_collections",
        "plex_url",
    )),
}


def _scrub_secrets(obj: Any) -> Any:
    """
    Walk ``obj`` and redact any value whose key matches a known secret
    name. Preserves overall dict / list structure. Booleans for the
    ``_encrypted`` marker are preserved as-is (they are not the
    secret; they are the marker that one was present).
    """
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if k == "_encrypted":
                out[k] = bool(v)
            elif k in _SECRET_FIELD_NAMES:
                if v in (None, "", False):
                    out[k] = v
                else:
                    out[k] = "<redacted>"
            else:
                out[k] = _scrub_secrets(v)
        return out
    if isinstance(obj, list):
        return [_scrub_secrets(v) for v in obj]
    return obj


def _format_value(v: Any) -> str:
    """
    Render a setting value as a single-line markdown-table cell.
    Lists become comma-joined strings; dicts become a JSON one-liner
    (compact, no trailing whitespace). Strings render as-is. None
    renders as the literal "null" so the end user can tell a missing
    key from an empty string.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v.replace("|", "\\|").replace("\n", " ").replace("\r", "")
    if isinstance(v, list):
        if not v:
            return "[]"
        return ", ".join(_format_value(x) for x in v)
    if isinstance(v, dict):
        try:
            return json.dumps(v, default=str, sort_keys=True)
        except Exception:
            return str(v)
    return str(v)


def _is_blank(v: Any) -> bool:
    """True when a value carries no end user-relevant information:
    None, empty string, empty list, empty dict. Used to decide
    whether a job param key gets hidden from the displayed table."""
    if v is None:
        return True
    if isinstance(v, str) and v == "":
        return True
    if isinstance(v, (list, dict)) and len(v) == 0:
        return True
    return False


def _build_tunables_rows() -> List[Tuple[str, Any, Any, bool]]:
    """
    Return ``[(key, current_value, default_value, is_overridden), ...]``
    sorted by key. Empty list on any failure to import / read.
    """
    try:
        from services import tunables
    except Exception:
        return []
    try:
        defaults = tunables.defaults()
    except Exception:
        return []
    rows: List[Tuple[str, Any, Any, bool]] = []
    for key in sorted(defaults):
        default_val = defaults[key]
        try:
            current_val = tunables.get(key)
        except Exception:
            current_val = default_val
        rows.append((key, current_val, default_val, current_val != default_val))
    return rows


def _build_persistent_dict() -> Dict[str, Any]:
    """
    Return the scrubbed top-level persistent settings dict (minus
    the ``tunables`` sub-document, rendered separately). Empty dict
    on any failure.
    """
    try:
        from server.persistence import load_settings
    except Exception:
        return {}
    try:
        raw = load_settings() or {}
    except Exception:
        return {}
    persistent = {k: v for k, v in raw.items() if k != "tunables"}
    return _scrub_secrets(persistent)


# ── At-a-glance helpers ───────────────────────────────────────────────

def _at_a_glance_lines(
    job_type: str,
    job_params: Dict[str, Any],
    persistent: Dict[str, Any],
) -> List[str]:
    """Build the 6-8 line summary that answers the end user's first
    question: 'what did this run do?'. Pulls from job_params first,
    falls back to persistent for defaults."""
    out: List[str] = []
    src = job_params.get("source_server_name")
    plex_url = job_params.get("plex_url")
    dests = job_params.get("dest_server_names") or job_params.get("dest_server_name")
    libs = job_params.get("libraries") or []
    workers = job_params.get("workers") or persistent.get("workers")
    scrobble_workers = (
        job_params.get("scrobble_workers")
        or persistent.get("scrobble_workers")
    )
    out_dir = job_params.get("output_dir") or persistent.get("output_dir")

    if src:
        line = f"Source:        {src}"
        if plex_url:
            line += f" ({plex_url})"
        out.append(line)
    if dests:
        if isinstance(dests, list):
            out.append(f"Destinations:  {', '.join(str(d) for d in dests)}")
        else:
            out.append(f"Destinations:  {dests}")
    if libs and isinstance(libs, list):
        out.append(f"Libraries ({len(libs)}): {', '.join(str(L) for L in libs)}")
    if out_dir and job_type in ("snapshot",):
        out.append(f"Output:        {out_dir}")
    if workers is not None:
        wline = f"Workers:       {workers}"
        if scrobble_workers is not None:
            wline += f" (scrobble {scrobble_workers})"
        out.append(wline)

    # A compact flags line covers the four most common run-shape
    # decisions in a single row.
    flags: List[str] = []
    if "strict_match" in job_params:
        flags.append(f"strict_match={_format_value(job_params['strict_match'])}")
    if "verbose" in job_params:
        flags.append(f"verbose={_format_value(job_params['verbose'])}")
    if "fast_collection_detection" in job_params:
        flags.append(
            f"fast_collection_detection={_format_value(job_params['fast_collection_detection'])}"
        )
    if "prebuild_json_sidecar" in job_params:
        flags.append(
            f"sidecar={_format_value(job_params['prebuild_json_sidecar'])}"
        )
    if "mode" in job_params:
        flags.append(f"mode={_format_value(job_params['mode'])}")
    if flags:
        out.append("Flags:         " + ", ".join(flags))

    return out


# ── Per-library metrics matrix ────────────────────────────────────────

_METRIC_KEYS = ("watch_history", "ratings", "playlists", "collections")
_METRIC_HEADERS = ("Watch", "Ratings", "Playlists", "Collections")


def _render_library_metrics_table(
    library_metrics: Any,
    libraries: Any,
) -> List[str]:
    """
    Render ``library_metrics`` as a per-library matrix with pipes
    aligned for plain-text viewing. Each metric column is widened to
    match its header so a viewer reading the .log in a monospace font
    sees a clean grid; the table is still valid GitHub-flavoured
    markdown (the ``:---:`` separators give centred alignment in
    rendered views).

    Returns an empty list when no map is present and no libraries are
    selected.
    """
    out: List[str] = []
    if not isinstance(library_metrics, dict) or not library_metrics:
        if isinstance(libraries, list) and libraries:
            out.append(
                f"_All four metrics ON for every selected library "
                f"({len(libraries)})._"
            )
        return out

    rows = sorted(library_metrics.keys())
    label_width = max(len("Library"), max(len(r) for r in rows))
    # Each metric column is at least 3 chars wide (" Y "/" - ") and at
    # least as wide as its header, so the pipes line up vertically in
    # both data and header rows.
    metric_widths = [max(len(h), 3) for h in _METRIC_HEADERS]

    header_cells = ["Library".ljust(label_width)] + [
        h.center(w) for h, w in zip(_METRIC_HEADERS, metric_widths)
    ]
    out.append("| " + " | ".join(header_cells) + " |")

    sep_cells = ["-" * label_width] + [
        ":" + "-" * (w - 2) + ":" for w in metric_widths
    ]
    out.append("| " + " | ".join(sep_cells) + " |")

    for name in rows:
        row = library_metrics[name] or {}
        cells: List[str] = [str(name).ljust(label_width)]
        for key, w in zip(_METRIC_KEYS, metric_widths):
            mark = "Y" if row.get(key, True) else "-"
            cells.append(mark.center(w))
        out.append("| " + " | ".join(cells) + " |")
    return out


# ── Persistent-settings grouping ──────────────────────────────────────

def _expand_dict_value(v: Any) -> List[str]:
    """For nested dict values (library_walk, restore_defaults,
    media_db_retention, transfer_resolution, ...), return one bullet
    per inner key. Returns a single bullet when the dict is empty so
    "{}" stays visible rather than disappearing."""
    if not isinstance(v, dict):
        return [_format_value(v)]
    if not v:
        return ["(empty)"]
    parts: List[str] = []
    for inner in sorted(v):
        parts.append(f"{inner}={_format_value(v[inner])}")
    return parts


def _render_persistent_groups(persistent: Dict[str, Any]) -> List[str]:
    """Group persistent settings by purpose. Unknown keys land in
    'Other' so adding a new setting to persistence.py never makes the
    field invisible. Nested dicts get expanded into one bullet per
    inner key."""
    used: set = set()
    out: List[str] = []

    def _emit_value(k: str, v: Any) -> None:
        if isinstance(v, dict):
            inner = _expand_dict_value(v)
            if len(inner) == 1 and inner[0] in ("(empty)",):
                out.append(f"- {k}: {inner[0]}")
            else:
                out.append(f"- {k}:")
                for line in inner:
                    out.append(f"  - {line}")
        else:
            out.append(f"- {k}: {_format_value(v)}")

    for label, keys in _PERSISTENT_GROUPS:
        present = [k for k in keys if k in persistent and k not in _PERSISTENT_SUPPRESS]
        if not present:
            continue
        out.append(f"### {label}")
        for k in present:
            _emit_value(k, persistent[k])
            used.add(k)
        out.append("")

    leftover = [
        k for k in sorted(persistent)
        if k not in used and k not in _PERSISTENT_SUPPRESS
    ]
    if leftover:
        out.append("### Other")
        for k in leftover:
            _emit_value(k, persistent[k])
        out.append("")

    return out


# ── Job-params rendering ──────────────────────────────────────────────

def _render_job_params(
    job_type: str,
    job_params: Dict[str, Any],
) -> List[str]:
    """Render the per-run params, hiding library_metrics (already
    above as a matrix) and blank fields, and noting how many
    other-mode keys were suppressed."""
    out: List[str] = []
    applicable = _JOB_PARAM_APPLICABLE.get(job_type, frozenset())
    scrubbed = _scrub_secrets(dict(job_params or {}))

    visible: List[Tuple[str, Any]] = []
    hidden_blank = 0
    hidden_other_mode = 0
    for k in sorted(scrubbed):
        if k == "library_metrics":
            # Promoted to its own matrix section above.
            continue
        if k == "libraries":
            # Already shown in At-a-glance; redundant here.
            continue
        v = scrubbed[k]
        if _is_blank(v):
            hidden_blank += 1
            continue
        if applicable and k not in applicable:
            hidden_other_mode += 1
            continue
        visible.append((k, v))

    if not visible:
        out.append("_No job params with values for this run._")
    else:
        out.append("| Key | Value |")
        out.append("|---|---|")
        for k, v in visible:
            out.append(f"| {k} | {_format_value(v)} |")

    if hidden_blank or hidden_other_mode:
        bits: List[str] = []
        if hidden_other_mode:
            bits.append(f"{hidden_other_mode} other-mode field(s) hidden")
        if hidden_blank:
            bits.append(f"{hidden_blank} null/empty field(s) hidden")
        out.append("")
        out.append(f"_({'; '.join(bits)})_")

    return out


# ── Top-level renderer ────────────────────────────────────────────────

def _render_markdown(
    *,
    captured_at_iso: str,
    job_type: str,
    run_log_dir: str,
    tunables_rows: List[Tuple[str, Any, Any, bool]],
    persistent: Dict[str, Any],
    job_params: Dict[str, Any],
) -> str:
    """Render the full markdown document as a single string."""
    out: List[str] = []
    scrubbed_params = _scrub_secrets(dict(job_params or {}))

    trigger = scrubbed_params.get("_trigger") or "unknown"
    run_dir_short = Path(run_log_dir).name if run_log_dir else ""

    out.append(f"# Run Settings - {run_dir_short or '(unknown run)'}")
    out.append("")
    out.append(f"Captured: {captured_at_iso}")
    out.append(f"Job: {job_type}  |  Trigger: {trigger}")
    out.append("")

    # At a glance.
    out.append("## At a glance")
    out.append("")
    glance = _at_a_glance_lines(job_type, scrubbed_params, persistent)
    if glance:
        out.append("```")
        out.extend(glance)
        out.append("```")
    else:
        out.append("_no summary fields available_")
    out.append("")

    # Per-library metrics matrix.
    out.append("## Per-library metrics")
    out.append("")
    matrix_lines = _render_library_metrics_table(
        scrubbed_params.get("library_metrics"),
        scrubbed_params.get("libraries"),
    )
    if matrix_lines:
        out.extend(matrix_lines)
    else:
        out.append("_no libraries selected_")
    out.append("")

    # Tunables.
    overridden = [r for r in tunables_rows if r[3]]
    out.append(
        f"## Tunables ({len(tunables_rows)} known, {len(overridden)} overridden)"
    )
    out.append("")
    if tunables_rows:
        if overridden:
            out.append("**Overridden:**")
            out.append("")
            out.append("| Key | Value | Default |")
            out.append("|---|---|---|")
            for key, current, default, _is in overridden:
                out.append(
                    f"| {key} | {_format_value(current)} | {_format_value(default)} |"
                )
            out.append("")
            out.append("**All tunables:**")
        else:
            out.append("_All tunables at built-in defaults._")
            out.append("")
            out.append("**Defaults in effect:**")
        out.append("")
        out.append("| Key | Value | Default | Override? |")
        out.append("|---|---|---|---|")
        for key, current, default, is_override in tunables_rows:
            marker = "yes" if is_override else "-"
            out.append(
                f"| {key} | {_format_value(current)} | "
                f"{_format_value(default)} | {marker} |"
            )
    else:
        out.append("_no tunables resolved (services.tunables unreachable)_")
    out.append("")

    # Persistent settings, grouped.
    out.append("## Persistent Settings")
    out.append("")
    if persistent:
        group_lines = _render_persistent_groups(persistent)
        if group_lines:
            out.extend(group_lines)
        else:
            out.append("_no settings to display_")
            out.append("")
    else:
        out.append("_no persistent settings resolved (server.persistence unreachable)_")
        out.append("")

    # Job params (mode-filtered).
    out.append("## Job Params")
    out.append("")
    job_lines = _render_job_params(job_type, scrubbed_params)
    out.extend(job_lines)
    out.append("")

    # Footer.
    out.append("---")
    out.append("")
    encrypted_marker = ""
    try:
        if bool(persistent.get("_encrypted")):
            encrypted_marker = " Persistent settings on disk are encrypted at rest."
    except Exception:
        pass
    out.append(
        "_Diagnostic dump only. The engine does not read this file. "
        "Secret-looking fields are redacted as `<redacted>`."
        + encrypted_marker + "_"
    )
    out.append("")
    return "\n".join(out)


def write_run_settings(
    *,
    run_log_dir: str,
    job_type: str,
    job_params: Optional[Dict[str, Any]] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[str]:
    """
    Write ``run-settings.log`` alongside the run log. Returns the
    written path on success, ``None`` when run logging is disabled
    (``run_log_dir`` empty) or when any step fails.

    Failures are logged at warning level on the supplied ``logger`` but
    never raised: the diagnostic dump is best-effort and must not
    impact the run.
    """
    if not run_log_dir:
        return None
    try:
        captured_at_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime()
        )
        tunables_rows = _build_tunables_rows()
        persistent = _build_persistent_dict()
        body = _render_markdown(
            captured_at_iso=captured_at_iso,
            job_type=str(job_type or "unknown"),
            run_log_dir=str(run_log_dir or ""),
            tunables_rows=tunables_rows,
            persistent=persistent,
            job_params=dict(job_params or {}),
        )
        path = Path(run_log_dir) / "run-settings.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        if logger is not None:
            try:
                logger.info(
                    "RUN SETTINGS: full settings snapshot written to %s",
                    path,
                )
            except Exception:
                pass
        return str(path)
    except Exception as exc:
        if logger is not None:
            try:
                logger.warning(
                    "Could not write run-settings.log under %s: %s",
                    run_log_dir, exc,
                )
            except Exception:
                pass
        return None
