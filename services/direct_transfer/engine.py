"""
Direct server-to-server transfer orchestrator.

Reads watch history, playlists, collections, and ratings from one
registered Plex server (the *source*) and writes them straight into
another registered server (the *destination*) without ever writing
an intermediate ``.plexexport.json`` to disk.

The engine's four-tier matching (GUID → exact filepath → suffix →
fuzzy title) runs against the destination just as it does in
file-mediated import mode. All additive-only merge rules also apply
unchanged - nothing on the destination is ever deleted, reduced, or
overwritten. Resume positions, ratings, playlists, collections, and
view counts are all merged via the same functions ``run_restore``
uses.

Engine constraint
-----------------
This module **does not** modify any engine source file. It calls
the existing primitives:

* From :mod:`services.snapshot.plex_native.snapshotter` -
  ``snapshot_watch_history``, ``snapshot_playlists``,
  ``snapshot_collections``, ``snapshot_ratings`` to build the data dict
  in memory.
* From :mod:`services.importer` -
  ``restore_export_file`` (called with ``preloaded_data=`` so it
  doesn't re-read from disk).

Both halves run under the existing ``DashboardState`` so the live
dashboard's per-library progress bar, activity feed, and counters
work identically to a normal snapshot-then-import sequence. The job
record's ``params`` carries ``source_server_name`` and
``dest_server_name`` so the WebSocket payload and the React
dashboard can render "Plex1 → Plex2" badges; no DashboardState
field is added.

One library at a time
---------------------
Direct transfer is intentionally serialised across libraries - each
library finishes gather→merge before the next starts. The engine
already parallelises *within* a library (four gather threads, then
multi-worker resolution+merge), and adding another layer of cross-
library parallelism would pile API load on two Plex servers
simultaneously, which the README's threading section explicitly
warns against.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from plexapi.server import PlexServer

import services.state as state
from services.dashboard import DashboardState
from services.snapshot.plex_native.snapshotter import (
    snapshot_collections,
    snapshot_playlists,
    snapshot_ratings,
    snapshot_watch_history,
)
from services.restore.plex_native import restore_export_file


log = logging.getLogger("plexmigrate.services.direct_transfer.engine")


class DirectTransferUnavailable(Exception):
    """
    Internal sentinel: raised when an attempted direct (in-memory)
    transfer cannot proceed and the orchestrator should switch to the
    chained snapshot-then-import fallback for the current library.

    Raised in two scenarios:
      * Pre-flight: one of the two registered servers does not respond
        to its identity ping within a short timeout.
      * Mid-flight: an exception escapes one of the export_* gather
        primitives or the restore_export_file call. We catch broadly
        and trust the fallback to either succeed or fail with a
        clearer error message.
    """


# ── Public entry point ───────────────────────────────────────────────────────

def run_direct_transfer(
    source_server: PlexServer,
    source_url: str,
    source_token: str,
    source_owner: str,
    dest_server: PlexServer,
    dest_url: str,
    dest_token: str,
    dest_owner: str,
    library_names: List[str],
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    stop_event: Optional[threading.Event] = None,
    output_dir: Optional[str] = None,
    source_home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    dest_home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    user_filter: Optional[List[str]] = None,
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    include_playlists: Optional[bool] = None,
    # Four-flag data-type filter. The legacy ``skip_*`` flags are
    # honoured alongside these include_* flags.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # Per-library metric filter. Forwarded into both the in-memory
    # transfer path (_transfer_one_library) and the chained-fallback
    # path (_chained_fallback_library) so library_metrics applies
    # end-to-end. Keys are library names; values are dicts with the
    # four boolean fields.
    library_metrics: Optional[Dict[str, Dict[str, bool]]] = None,
    # Restore mode forwarded into restore_export_file by both the
    # in-memory path (_transfer_one_library) and the chained-fallback
    # path (_chained_fallback_library). Default "merge" = additive;
    # "replace" = destructive point-in-time. The job worker in
    # server/jobs.py handles the pre-Replace safety-belt snapshot
    # and the confirm_replace gate before calling this.
    mode: str = "merge",
    # Merge sub-strategy for watch-count math. "higher" = destination
    # ends at max(stored, current); "sum" = destination ends at
    # current + stored (end user opt-in). Ignored when mode=="replace".
    merge_watch_strategy: str = "higher",
    # Per-run override that bypasses the library mapping table. False
    # (default) = consult the saved mapping table the same way the
    # snapshot-restore path does, so unmapped libraries route to their
    # declared equivalents and unmappable libraries get skipped with
    # a clear warning. True = exact-name match only, no table lookup.
    # Surfaced as a first-class checkbox on the Run Job form;
    # resolution happens at the top of ``run_direct_transfer``'s
    # per-library loop via ``library_mapping_lookup.resolve_for_one_library``,
    # which is the same helper the snapshot-restore engine uses.
    ignore_library_mapping: bool = False,
    # Per-run source-name → dest-name overrides. Same shape as
    # ``run_restore``'s matching kwarg. Beats both the saved mapping
    # table and the ``ignore_library_mapping`` flag for any source name
    # that has an entry (the empty string is a deliberate per-run
    # skip). Resolved at the top of the per-library loop via the
    # shared ``resolve_for_one_library`` helper, then the resolved
    # destination name is plumbed into restore_export_file via
    # ``target_section_name_override`` so the per-library payload
    # lands in the correct destination library (source name is kept
    # in the payload's ``library`` field for source-side DB ingest).
    library_mapping_overrides: Optional[Dict[str, str]] = None,
    # Server IDs (added so direct_transfer can thread them through to
    # restore_export_file's library_mappings lookup). Optional for
    # backward compat with callers that don't have them; without
    # both IDs the same-server short-circuit / mapping consult cannot
    # fire and the path degrades to exact-name match.
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
) -> None:
    """
    Drive an end-to-end direct transfer.

    Workflow per library:
        1. Find the source library section by friendly name.
        2. Resolve source library name → destination library name via
           ``library_mapping_lookup.resolve_for_one_library`` (same
           helper the snapshot-restore engine uses). Resolution order:
           per-run ``library_mapping_overrides`` entry first; then
           ``ignore_library_mapping`` / same-server short-circuit for
           exact-name-only; otherwise consult the saved
           ``library_mappings`` table (operator → exact → auto).
        3. Run the four snapshot gather primitives against the source
           into an in-memory dict matching the .plexexport.json shape.
        4. Hand that dict to ``restore_export_file`` against the
           destination via ``preloaded_data``, with
           ``target_section_name_override`` set to the resolved
           destination name so the renamed library is the write target.

    Args:
        source_server / source_url / source_token / source_owner:
            Connection info for the source Plex.
        dest_server   / dest_url   / dest_token   / dest_owner:
            Connection info for the destination Plex.
        library_names: Friendly names to transfer. Empty list = transfer
            every library that exists on *both* servers.
        logger: Shared logger (per-run handler set up by the job runner).
        log_dir: Run log directory path (string).
        remap: Optional path-prefix translation tuple for cross-OS migrations.
        strict_match: Engine flag - same semantics as run_restore.
        stop_event: Optional Event the job worker flips when the user
            clicks "Stop Job". The transfer drains the current library
            and exits cleanly when set.
    """
    # Resolve the library list. If the caller passed an empty list we
    # take "all libraries present on both servers" rather than "all on
    # source" - transferring a library that doesn't exist on the
    # destination just produces an entire run's worth of unresolved
    # items, which isn't useful.
    src_by_name = {s.title: s for s in source_server.library.sections()}
    dst_by_name = {s.title: s for s in dest_server.library.sections()}
    dst_titles: Set[str] = set(dst_by_name)

    if not library_names:
        # Auto-discover: union of (exact name on both) ∪ (source
        # libraries that the per-run override map routes somewhere
        # viable on the dest). The mapping table itself is consulted
        # per-library at section-resolution time below; here we only
        # widen the implicit list when the operator supplied an
        # override map so an unmapped library is genuinely transferred.
        library_names = sorted(
            set(src_by_name) & set(dst_by_name)
            | {
                src_name
                for src_name, dst_name in (library_mapping_overrides or {}).items()
                if src_name in src_by_name and dst_name and dst_name in dst_titles
            }
        )
        if not library_names:
            raise ValueError(
                "No libraries exist on both servers - nothing to transfer."
            )

    # P0-1: clear per-job accumulators (success/failure dicts) so a long-
    # lived server process doesn't carry totals from prior jobs into this
    # transfer's troubleshoot.log.
    state.reset_run_state()

    # Transfer-resolution policy. The four-tier resolver in
    # services/resolver.py reads ``state._resolver_allow_filepath`` and
    # ``state._resolver_allow_fuzzy`` at every resolve_item call. Set
    # them from settings.transfer_resolution here so the policy is
    # scoped to this transfer only - reset_run_state() restores
    # permissive defaults afterward, leaving snapshot/import paths
    # unaffected. The default for fuzzy here is FALSE (opt-in), the
    # opposite of the snapshot/import default - per the end user-set
    # spec, fuzzy matches in a destination-writing path are too risky
    # to enable by default.
    try:
        from server.persistence import load_settings as _load_settings
        _tr = (_load_settings() or {}).get("transfer_resolution") or {}
        state._resolver_allow_filepath = bool(_tr.get("allow_filepath_fallback", True))
        state._resolver_allow_fuzzy = bool(_tr.get("allow_fuzzy_fallback", False))
        logger.info(
            "Transfer resolution: filepath=%s fuzzy=%s",
            state._resolver_allow_filepath, state._resolver_allow_fuzzy,
        )
    except Exception:
        # Settings hiccup falls back to the conservative
        # "filepath on, fuzzy off" pair so the transfer doesn't
        # accidentally enable risky matching on a missing-config
        # path.
        state._resolver_allow_filepath = True
        state._resolver_allow_fuzzy = False
        logger.warning(
            "Could not load transfer_resolution settings; "
            "defaulting to filepath=True, fuzzy=False.",
        )

    # ── Dashboard setup ──────────────────────────────────────────────
    # The job runner sets state._run_timestamp before calling us so the
    # log dir already carries a "Src-to-Dst" prefix. We just populate
    # DashboardState - the same one the WebSocket reads at 4 Hz.
    # Augment any placeholder the job runner created so pre-flight
    # activity entries (Plex source/dest connects) are preserved.
    if state.get_dashboard() is None:
        state._dashboard = DashboardState(log_dir=log_dir)
    else:
        state.get_dashboard().log_dir = log_dir

    # Normalize the two flags into one internal include flag. When
    # ``include_playlists`` is supplied explicitly, use it. Otherwise
    # derive it from the ``skip_playlists`` boolean (False ⇒ include,
    # True ⇒ exclude). The downstream workers only see
    # ``include_playlists`` so the gating logic doesn't need to know
    # about the ``skip_playlists`` field at all.
    if include_playlists is None:
        include_playlists = not skip_playlists
    # ``skip_collections`` similarly disables the collection phase;
    # honour both inputs.
    if skip_collections:
        include_collections = False

    # One-line summary of which data types this run will transfer. The
    # per-library skip notices are DEBUG; this is the only INFO line
    # confirming the end user's filter choices for the direct path.
    _included = [
        n for n, v in (
            ("watch_history", include_watch_history),
            ("ratings",       include_ratings),
            ("playlists",     include_playlists),
            ("collections",   include_collections),
        ) if v
    ]
    logger.info(
        "Direct transfer data types: %s",
        ", ".join(_included) if _included else "(none)",
    )
    # Restore-mode header. Same forensic-trail rationale as the
    # matching log line in services.restore.plex_native.run_restore - the mode +
    # sub-strategy are recorded at INFO so runtime.log makes it
    # obvious whether a destination was additive-merged or
    # destructively-replaced.
    if mode == "replace":
        logger.info("Direct transfer mode: REPLACE (destination overwritten).")
    else:
        if merge_watch_strategy == "sum":
            logger.info(
                "Direct transfer mode: merge (additive, watch counts COMBINE current+stored)."
            )
        else:
            logger.info(
                "Direct transfer mode: merge (additive, watch counts keep HIGHER of stored/current)."
            )

    # ── Per-user data + owner ──────────────────────────────────────
    # Compute the effective per-user roster. Managed-user data
    # propagates when the same managed username exists on both source
    # and destination, and the owner is a first-class filter target -
    # when the end user unchecks the owner the engine skips the entire
    # library-level ``payload["items"]`` block (watch_history,
    # playlists, library-level collections, ratings) for that run.
    #
    # Inputs:
    #   - source_home_users : (uname, src_token, src_user_server) tuples
    #   - dest_home_users   : (uname, dst_token, dst_user_server) tuples
    #   - user_filter       : end user's checked-list of raw Plex
    #                         identifiers. None = include every
    #                         transferable user including the owner;
    #                         explicit list = only the ones named.
    #                         Empty list = no users - the run is a no-op.
    src_users_by_name: Dict[str, Tuple[str, str, PlexServer]] = {
        u[0]: u for u in (source_home_users or [])
    }
    dst_users_by_name: Dict[str, Tuple[str, str, PlexServer]] = {
        u[0]: u for u in (dest_home_users or [])
    }
    transferable: Set[str] = set(src_users_by_name) & set(dst_users_by_name)

    # Owner is NEVER a home user in Plex's API model
    # (account.users() returns managed + linked accounts only).
    # The end user's filter list ships with the owner's email
    # inline alongside managed usernames because the Run Job form
    # presents them as one checkbox grid - but the backend must
    # treat them as two separate concepts:
    #
    #   * Owner inclusion is a BOOLEAN. The owner's data always
    #     comes along with the base library calls (no separate
    #     authentication path), so "include the owner" means
    #     "process the library's own items[] block." When the
    #     end user unchecks the owner, we skip that block. That's
    #     it - no API lookup involved.
    #
    #   * ``user_filter`` is a list of MANAGED-USER IDENTIFIERS
    #     only. It feeds into the home-users validation
    #     (``managed_filter & transferable``) and the per-user
    #     gather loop. The owner email must never enter this
    #     set, or every direct transfer with the owner checked
    #     spits a spurious "not present on both servers" warning.
    #
    # We get the source owner's email directly from the source
    # PlexServer (``myPlexAccount().email``) instead of
    # ``state._plex_owner_email``. The state version isn't
    # propagated through ContextVars to fan-out destination
    # threads - each destination starts with a fresh context and
    # the email field is empty there. plexapi caches the
    # ``myPlexAccount`` instance on first call so this is free
    # on subsequent reads. Read once into a local, use that local
    # for the rest of the function.
    try:
        source_owner_email = (
            (source_server.myPlexAccount().email or "").strip()
        )
    except Exception as exc:
        # Local-admin tokens / Plex.tv unreachable - fall back to
        # whatever state may have populated (CLI mode does it).
        # If state is also empty, treat as "owner email unknown"
        # and the end user's box stays the source of truth: an
        # explicit empty user_filter is the only way to exclude
        # the owner, and any list-of-managed-users is treated
        # as "owner included" since we can't verify otherwise.
        source_owner_email = (state._plex_owner_email or "").strip()
        if not source_owner_email:
            logger.debug(
                "Source owner email unavailable (%s); user_filter "
                "validation will not be able to strip owner email "
                "from a filter list. Owner inclusion still works "
                "via ``user_filter is None``.",
                exc,
            )

    # Strip the owner email from the validation set BEFORE any
    # comparison runs. The check is at the API boundary -
    # everything downstream sees a managed-user-only filter.
    if user_filter is None:
        included: Set[str] = set(transferable)
        managed_filter: Set[str] = set()
        owner_in_raw_filter = True  # absence of filter ⇒ include all
    else:
        owner_in_raw_filter = (
            bool(source_owner_email) and source_owner_email in set(user_filter)
        )
        managed_filter = {
            u for u in user_filter
            if not (source_owner_email and u == source_owner_email)
        }
        included = managed_filter & transferable
        missing = managed_filter - transferable
        if missing:
            logger.warning(
                "user_filter requested managed user(s) %s not present "
                "on both servers; skipped.",
                sorted(missing),
            )

    # ``owner_included`` is the single boolean the rest of this
    # function uses to decide whether to walk the owner block. It's
    # computed from the unfiltered ``user_filter`` value above
    # (captured into ``owner_in_raw_filter`` while we still had
    # access to the raw list) so the email-strip step doesn't
    # accidentally erase the owner's checkbox state.
    if source_owner_email:
        owner_included = owner_in_raw_filter
    else:
        # Owner email unknown - be permissive: assume owner is
        # included unless the end user sent an explicit empty
        # ``user_filter = []`` (which already means "no one").
        owner_included = (user_filter is None) or bool(user_filter)

    # Build the ordered, filtered home_users lists each side will see.
    effective_src_home = [src_users_by_name[n] for n in sorted(included)]
    effective_dst_home = [dst_users_by_name[n] for n in sorted(included)]

    # Actionable run-log line when the owner is unchecked, so the end
    # user sees exactly what they're skipping.
    if not owner_included:
        logger.info(
            "[user-filter] Owner excluded - library-level collections "
            "will not transfer this run. Personal collections for "
            "included users will transfer normally.",
        )

    # One INFO line at transfer start so the end user can audit what
    # went where straight from the run log. The message reflects the
    # actual ``owner_included`` state so it never contradicts the
    # "Owner excluded" line above.
    logger.info(
        "Direct transfer per-user scope: owner %s; "
        "%d managed user(s) included: %s",
        "included" if owner_included else "EXCLUDED",
        len(included),
        sorted(included) if included else "none",
    )

    # User count for the dashboard reflects the actual roster: owner
    # counts as 1 only when owner_included is True, so the dashboard's
    # home_user_count stays correct on owner-excluded runs.
    state.get_dashboard().set_user_count(
        (1 if owner_included else 0) + len(included)
    )
    for lib_name in library_names:
        # Each library does 4 gather phases + N resolution phases.
        # We use a coarse total of 8 (4 gather + 4 import) so the bar
        # animates smoothly without inflating to a confusing "1273 items".
        state.get_dashboard().add_library(lib_name, total=8)
        state.get_dashboard().set_library_status(lib_name, "queued")
    state._live_instance = None  # No Rich Live in server mode.

    # Resolve where the chained-fallback temp files (if any) land.
    # Falls back to ``./snapshots`` to match the engine default
    # used by snapshot_library when no output_dir is configured.
    resolved_output_dir = output_dir or "./snapshots"
    try:
        for lib_name in library_names:
            if stop_event is not None and stop_event.is_set():
                logger.info("Stop requested - skipping remaining libraries.")
                state.get_dashboard().set_library_status(lib_name, "queued")
                continue

            src_section = src_by_name.get(lib_name)
            if src_section is None:
                logger.warning(f"Library {lib_name!r} not found on source - skipping.")
                state.get_dashboard().finish_library(lib_name, error=True)
                continue

            # Resolve source library name → destination library name via
            # the shared mapping helper. The same helper runs on the
            # snapshot-restore path (services.restore.plex_native.engine.run_restore)
            # so a snapshot-then-restore and an in-memory direct transfer
            # pick the SAME destination library for any given source
            # library. Inputs: the per-run overrides (Run Job form), the
            # ignore-mapping flag (power-user override), the source/dest
            # server IDs (consulted by the saved-mapping table lookup),
            # and the live destination titles. Outcome is one of
            # OK / SKIP / NO_MATCH; only OK proceeds.
            from services.library_mapping.lookup import (
                resolve_for_one_library,
                RESOLVE_OK,
                RESOLVE_SKIP,
            )
            _src_lib_id = str(getattr(src_section, "key", "") or "")
            _status, _dest_lib_name = resolve_for_one_library(
                source_library_name=lib_name,
                source_library_id=_src_lib_id,
                source_server_id=source_server_id or "",
                dest_server_id=dest_server_id or "",
                dest_library_names=dst_titles,
                library_mapping_overrides=library_mapping_overrides,
                ignore_library_mapping=ignore_library_mapping,
                logger=logger,
            )
            if _status == RESOLVE_SKIP:
                # Operator-confirmed (or per-run override) explicit skip.
                # Mark the dashboard library as done with no error so the
                # run summary reflects an intentional skip, not a failure.
                state.get_dashboard().finish_library(lib_name)
                continue
            if _status != RESOLVE_OK or _dest_lib_name is None:
                logger.warning(
                    "Library %r has no destination counterpart "
                    "(destination has: %r). Skipping; either rename the "
                    "library on the destination to match, set up a Library "
                    "Mapping under Servers, or supply a per-run override.",
                    lib_name, sorted(dst_titles),
                )
                state.get_dashboard().finish_library(lib_name, error=True)
                continue

            dst_section = dst_by_name.get(_dest_lib_name)
            if dst_section is None:
                # Defensive: helper already validated against dst_titles
                # but the underlying section dict might be out of sync
                # if Plex changed mid-run.
                logger.warning(
                    f"Library {_dest_lib_name!r} resolved as destination for "
                    f"source {lib_name!r} but is no longer present - skipping."
                )
                state.get_dashboard().finish_library(lib_name, error=True)
                continue

            # Attempt the in-memory direct transfer first. If it fails
            # for any reason (network blip mid-gather, OOM, unexpected
            # API response, etc.), fall back to a chained
            # snapshot-then-import for this library without aborting
            # the whole job. The user sees the fallback in the
            # activity feed; the end result is identical either way.
            try:
                _transfer_one_library(
                    lib_name=lib_name,
                    dest_lib_name=_dest_lib_name,
                    src_section=src_section,
                    source_server=source_server,
                    source_owner=source_owner,
                    dest_server=dest_server,
                    dest_url=dest_url,
                    dest_token=dest_token,
                    logger=logger,
                    log_dir=log_dir,
                    remap=remap,
                    strict_match=strict_match,
                    stop_event=stop_event,
                    source_home_users=effective_src_home,
                    dest_home_users=effective_dst_home,
                    owner_included=owner_included,
                    skip_collections=skip_collections,
                    fast_collection_detection=fast_collection_detection,
                    skip_playlists=skip_playlists,
                    include_playlists=include_playlists,
                    include_watch_history=include_watch_history,
                    include_ratings=include_ratings,
                    include_collections=include_collections,
                    mode=mode,
                    merge_watch_strategy=merge_watch_strategy,
                    # Per-library metric override map.
                    library_metrics=library_metrics,
                    # Forward to the underlying restore_export_file
                    # call so direct transfer honours the mapping
                    # table identically to the snapshot-restore path.
                    # Plus the per-run override map.
                    ignore_library_mapping=ignore_library_mapping,
                    library_mapping_overrides=library_mapping_overrides,
                    source_server_id=source_server_id,
                    dest_server_id=dest_server_id,
                )
            except DirectTransferUnavailable as exc:
                logger.warning(
                    "Direct transfer unavailable for %r (%s) - falling back "
                    "to chained snapshot-then-import.", lib_name, exc,
                )
                state.get_dashboard().push_activity(
                    "phase", lib_name,
                    f"Direct path unavailable: {exc} - falling back to chained.",
                )
                _chained_fallback_library(
                    lib_name=lib_name,
                    dest_lib_name=_dest_lib_name,
                    src_section=src_section,
                    source_server=source_server,
                    source_owner=source_owner,
                    dest_server=dest_server,
                    dest_url=dest_url,
                    dest_token=dest_token,
                    logger=logger,
                    log_dir=log_dir,
                    remap=remap,
                    strict_match=strict_match,
                    output_dir=resolved_output_dir,
                    stop_event=stop_event,
                    source_home_users=effective_src_home,
                    dest_home_users=effective_dst_home,
                    owner_included=owner_included,
                    skip_collections=skip_collections,
                    fast_collection_detection=fast_collection_detection,
                    skip_playlists=skip_playlists,
                    include_playlists=include_playlists,
                    include_watch_history=include_watch_history,
                    include_ratings=include_ratings,
                    include_collections=include_collections,
                    mode=mode,
                    merge_watch_strategy=merge_watch_strategy,
                    # Per-library metric override map.
                    library_metrics=library_metrics,
                    # Forward to the underlying restore_export_file
                    # call inside the fallback. Plus the per-run
                    # override map.
                    ignore_library_mapping=ignore_library_mapping,
                    library_mapping_overrides=library_mapping_overrides,
                    source_server_id=source_server_id,
                    dest_server_id=dest_server_id,
                )
            except Exception as exc:
                # Any unexpected exception in the direct path: log and
                # try the chained fallback. We swallow the original
                # exception type because the user's report will come
                # from the chained path's own error if it also fails.
                logger.exception(
                    "Direct transfer of %r failed unexpectedly - "
                    "attempting chained fallback.", lib_name,
                )
                state.get_dashboard().push_activity(
                    "phase", lib_name,
                    "Direct path errored - falling back to chained snapshot-then-import.",
                )
                _chained_fallback_library(
                    lib_name=lib_name,
                    dest_lib_name=_dest_lib_name,
                    src_section=src_section,
                    source_server=source_server,
                    source_owner=source_owner,
                    dest_server=dest_server,
                    dest_url=dest_url,
                    dest_token=dest_token,
                    logger=logger,
                    log_dir=log_dir,
                    remap=remap,
                    strict_match=strict_match,
                    output_dir=resolved_output_dir,
                    stop_event=stop_event,
                    source_home_users=effective_src_home,
                    dest_home_users=effective_dst_home,
                    owner_included=owner_included,
                    skip_collections=skip_collections,
                    fast_collection_detection=fast_collection_detection,
                    skip_playlists=skip_playlists,
                    include_playlists=include_playlists,
                    include_watch_history=include_watch_history,
                    include_ratings=include_ratings,
                    include_collections=include_collections,
                    mode=mode,
                    merge_watch_strategy=merge_watch_strategy,
                    # Per-library metric override map.
                    library_metrics=library_metrics,
                    # Forward to the underlying restore_export_file
                    # call inside the fallback. Plus the per-run
                    # override map.
                    ignore_library_mapping=ignore_library_mapping,
                    library_mapping_overrides=library_mapping_overrides,
                    source_server_id=source_server_id,
                    dest_server_id=dest_server_id,
                )
            state.get_dashboard().finish_library(lib_name)

        # Replace-mode dest-only playlist sweep - scoped by destination
        # LIBRARY KEY (not the playlistType family). Without library-
        # level scoping, a Replace direct-transfer where source has ONE
        # audio library ("Music") and destination has TWO ("Music" +
        # "Audio Files") would delete playlists from the dest-only
        # library because both libraries share ``playlistType="audio"``.
        # Library-level scoping only considers playlists whose items
        # all live inside libraries explicitly chosen to transfer.
        #
        # Two passes:
        #   * Owner pass via admin-authenticated source/dest servers.
        #   * Per-user pass via each home user's user-authenticated
        #     PlexServer. Plex playlists are owned by the user that
        #     created them; the per-user pass only deletes that user's
        #     own dest-only playlists.
        if (
            mode == "replace"
            and (include_playlists is not False)
            and not skip_playlists
        ):
            try:
                # Destination-side library section keys we actually
                # transferred. Anything outside this set is preserved.
                restored_library_keys: Set[str] = set()
                for ln in library_names:
                    sec = dst_by_name.get(ln)
                    if sec is None:
                        continue
                    key = getattr(sec, "key", None)
                    if key is not None:
                        restored_library_keys.add(str(key))
                # Source titles in scope: only playlists whose items
                # live entirely within the source-side libraries we
                # transferred. This prevents the sweep from carrying
                # over a stray title from an unrelated source-side
                # library (mirror of the dest-side scope check).
                source_library_keys_src: Set[str] = set()
                for ln in library_names:
                    sec = src_by_name.get(ln)
                    if sec is None:
                        continue
                    key = getattr(sec, "key", None)
                    if key is not None:
                        source_library_keys_src.add(str(key))

                def _src_titles_in_scope(plex_server) -> Set[str]:
                    titles: Set[str] = set()
                    for pl in plex_server.playlists():
                        try:
                            item_lib_keys: Set[str] = set()
                            empty = True
                            for it in (pl.items() or []):
                                empty = False
                                lib_key = getattr(it, "librarySectionID", None)
                                if lib_key is None:
                                    item_lib_keys = set()
                                    break
                                item_lib_keys.add(str(lib_key))
                            if empty or not item_lib_keys:
                                continue
                            if not item_lib_keys.issubset(source_library_keys_src):
                                continue
                            t = getattr(pl, "title", "") or ""
                            if t:
                                titles.add(t)
                        except Exception:
                            continue
                    return titles

                from services.restore.plex_native import purge_dest_only_playlists

                # Owner sweep.
                deleted = purge_dest_only_playlists(
                    dest_server,
                    _src_titles_in_scope(source_server),
                    restored_library_keys,
                    logger,
                )
                if deleted:
                    logger.info(
                        "Replace: removed %d destination-only owner "
                        "playlist(s) absent from source.", deleted,
                    )
                # Per-managed-user sweep. ``effective_src_home`` and
                # ``effective_dst_home`` are aligned to the same sorted
                # username list above so positional pairing is safe.
                user_pairs = zip(
                    effective_src_home or [], effective_dst_home or [],
                )
                for (src_name, _src_tok, src_user_server), (dst_name, _dst_tok, dst_user_server) in user_pairs:
                    if src_name != dst_name:
                        # Defensive: shouldn't happen given the sorted
                        # pairing above, but skip rather than risk
                        # cross-user deletion.
                        continue
                    try:
                        deleted = purge_dest_only_playlists(
                            dst_user_server,
                            _src_titles_in_scope(src_user_server),
                            restored_library_keys,
                            logger,
                        )
                        if deleted:
                            logger.info(
                                "Replace: removed %d destination-only "
                                "playlist(s) for managed user %r absent "
                                "from source.", deleted, dst_name,
                            )
                    except Exception:
                        logger.exception(
                            "Replace: per-user dest-only playlist sweep "
                            "failed for %r; continuing.", dst_name,
                        )
            except Exception:
                logger.exception(
                    "Replace: dest-only playlist sweep failed; "
                    "continuing without it."
                )
    finally:
        # Leave DashboardState in place for ~3 s so the WebSocket
        # broadcaster gets one more snapshot with the final counters.
        # The job worker clears it after a small grace period.
        pass


# ── Import-total estimator ────────────────────────────────────────────────────

def _compute_import_total(
    payload: Dict[str, Any],
    *,
    home_user_names: Set[str],
) -> int:
    """
    Estimate the per-item / per-row work the import side will do for
    one library's payload.

    Mirrors :func:`services.restore.plex_native.run_restore`'s own ``_peek_metadata``
    arithmetic so direct-transfer's progress bar fills the same way a
    standalone import would. Returns 0 on a fully-empty payload; the
    caller adds the snapshot-phase constant (4) on top.

    Only counts users that exist on the destination side
    (``home_user_names``) - users on the source but missing on the
    destination produce no work on the import path. ``_restore_user``
    already short-circuits for them, so including their items would
    inflate the total and prevent the bar from reaching 100%.
    """
    # Unified users map. Owner is identified by role='owner' and is
    # always counted (we always restore the owner block);
    # managed users are counted only when the destination has a matching
    # home user (otherwise their work is skipped at restore time).
    users_block = payload.get("users", {}) or {}
    total = 0
    for handle, udata in users_block.items():
        if not isinstance(udata, dict):
            continue
        role = udata.get("role") or ("owner" if handle == "" else "managed")
        if role != "owner" and handle not in home_user_names:
            continue
        total += len(udata.get("watch_history", []) or [])
        total += sum(len(pl.get("items", []) or []) for pl in (udata.get("playlists", []) or []))
        total += len(udata.get("playlists", []) or [])
        total += sum(len(c.get("items", []) or []) for c in (udata.get("collections", []) or []))
        total += len(udata.get("collections", []) or [])
        total += len(udata.get("ratings", []) or [])
    return total


# ── Per-user gather ───────────────────────────────────────────────────────────

def _gather_users_data(
    lib_name: str,
    source_home_users: List[Tuple[str, str, PlexServer]],
    owner_coll_keys: Set[Any],
    logger: logging.Logger,
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    # Four-flag data-type filter. ``include_playlists`` is honoured
    # alongside ``skip_playlists`` (either disables).
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    Build the ``payload["users"]`` dict for one library by reading each
    source-side managed user's watch history, ratings, playlists, and
    personal collections.

    Shared by the in-memory direct path and the chained-fallback path
    so both produce identical per-user payloads. Best-effort: a user
    whose library section can't be resolved (no visibility, deleted)
    is logged and skipped rather than aborting the whole transfer.
    Empty input list returns an empty dict.

    ``owner_coll_keys`` is the rating-key set of the owner-side
    library collections - used to subtract library-level collections
    from each user's set so only genuinely-personal collections land
    in the per-user payload.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not source_home_users:
        return out
    for (uname, _user_token, user_server) in source_home_users:
        try:
            user_section = next(
                (s for s in user_server.library.sections() if s.title == lib_name),
                None,
            )
            if user_section is None:
                logger.info(
                    "Direct transfer: library %r not visible to source user %r - "
                    "their data for this library is skipped.",
                    lib_name, uname,
                )
                continue
            # Each include_* flag gates the corresponding per-user
            # gather independently.
            u_watch = (
                snapshot_watch_history(user_section, logger, user=uname)
                if include_watch_history else []
            )
            u_ratings = (
                snapshot_ratings(user_section, logger, user=uname)
                if include_ratings else []
            )
            u_playlists = (
                snapshot_playlists(
                    user_server, user_section.key, logger, lib_name=lib_name,
                    skip_playlists=skip_playlists,
                )
                if include_playlists else []
            )
            # Personal collections using the same three-layer
            # optimisation as the snapshotter's gather_user.
            # skip_rating_keys handles early-exit + per-item dedup;
            # fast_owner_detection uses librarySectionUserID when available.
            u_collections = (
                [] if (skip_collections or not include_collections)
                else snapshot_collections(
                    user_section, logger,
                    skip_rating_keys=owner_coll_keys,
                    fast_owner_detection=fast_collection_detection,
                )
            )
            out[uname] = {
                "watch_history": u_watch,
                "ratings": u_ratings,
                "playlists": u_playlists,
                "collections": u_collections,
            }
            logger.info(
                "Direct transfer: gathered source user %r - %d watched, "
                "%d rated, %d playlist(s), %d personal collection(s) for %r.",
                uname, len(u_watch), len(u_ratings), len(u_playlists),
                len(u_collections), lib_name,
            )
        except Exception as e:
            logger.warning(
                "Direct transfer: could not gather data for source user %r in %r: %s",
                uname, lib_name, e,
            )
    return out


# ── Per-library worker ───────────────────────────────────────────────────────

def _transfer_one_library(
    *,
    lib_name: str,
    src_section: Any,
    source_server: PlexServer,
    source_owner: str,
    dest_server: PlexServer,
    dest_url: str,
    dest_token: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    stop_event: Optional[threading.Event] = None,
    source_home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    dest_home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    owner_included: bool = True,
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    include_playlists: bool = True,
    # Additional include_* flags. ``include_playlists`` already
    # arrives normalised from ``run_direct_transfer``.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # Restore mode (merge / replace). Forwarded into
    # restore_export_file when this function calls it below.
    mode: str = "merge",
    merge_watch_strategy: str = "higher",
    # Per-library metric map. When this library has an entry, the
    # entry's flags override the include_* booleans for this library
    # only.
    library_metrics: Optional[Dict[str, Dict[str, bool]]] = None,
    # Pass-through for the per-run library-mapping bypass + the server
    # IDs that the restorer needs to consult the mapping table. Plus
    # the per-run override map.
    ignore_library_mapping: bool = False,
    library_mapping_overrides: Optional[Dict[str, str]] = None,
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
    # Destination library name (after mapping resolution at the caller
    # boundary in ``run_direct_transfer``). When None / unset, defaults
    # to ``lib_name`` so legacy direct callers (without mapping support)
    # keep working unchanged.
    dest_lib_name: Optional[str] = None,
) -> None:
    """
    Transfer a single library source→dest. See :func:`run_direct_transfer`
    for the high-level contract.
    """
    # ``dest_lib_name`` is the post-mapping destination library title.
    # All source-side reads use ``lib_name``; all destination-side
    # writes use ``dest_lib_name``. When the caller didn't resolve a
    # mapping (legacy code paths), default to lib_name so the
    # destination lookup behaves the same as before.
    if not dest_lib_name:
        dest_lib_name = lib_name
    # Per-library metric override. Same pattern as in the
    # snapshotter / restorer entry points - apply the map once at the
    # top of the function so every downstream local include_* read
    # gets the per-library value automatically.
    if library_metrics and lib_name in library_metrics:
        _lm_row = library_metrics[lib_name]
        if isinstance(_lm_row, dict):
            include_watch_history = bool(_lm_row.get("watch_history", include_watch_history))
            include_ratings = bool(_lm_row.get("ratings", include_ratings))
            include_playlists = bool(_lm_row.get("playlists", include_playlists))
            include_collections = bool(_lm_row.get("collections", include_collections))
            logger.info(
                "Per-library metric override (direct) for %r: "
                "watch_history=%s ratings=%s playlists=%s collections=%s",
                lib_name, include_watch_history, include_ratings, include_playlists, include_collections,
            )
    state.get_dashboard().set_library_status(lib_name, "active")
    state.get_dashboard().set_library_phase(lib_name, "Reading source…")
    state.get_dashboard().push_activity(
        "started", lib_name, f"Direct transfer: source → dest",
    )

    # ── Phase 1: gather data from the source ─────────────────────────
    # We are deliberately not running the four gather functions in
    # parallel here. The engine itself parallelises calls underneath
    # (e.g. multi-threaded HTTP) and stacking another concurrency
    # layer would interleave threads from two Plex servers in
    # unpredictable ways. The user can request a higher worker count
    # via --workers if they want more parallelism.
    #
    # Temporarily set the global owner name so per-item log lines
    # attribute correctly. ``state._plex_owner_name`` is read by the
    # snapshotter functions to label each item.
    saved_owner = state._plex_owner_name
    state._plex_owner_name = source_owner
    # Gate the owner-side gather on the end user's filter choice. When
    # the owner is unchecked we still advance the phase counter (the
    # per-library dashboard total reserves 4 ticks for the snapshot
    # side) but skip the four export_* calls - the library-level
    # items[] block lands empty and the import side's owner-scoped
    # path becomes a no-op.
    try:
        if owner_included:
            # Gate each owner-side gather on its include_* flag. The
            # corresponding phase counter still advances so the
            # per-library bar tracks consistently regardless of which
            # types are migrated.
            watch_history = (
                snapshot_watch_history(src_section, logger, user=source_owner)
                if include_watch_history else []
            )
            state.get_dashboard().advance_library(lib_name, 1)
            state.get_dashboard().set_library_phase(lib_name, "Reading playlists…")

            playlists = (
                snapshot_playlists(source_server, src_section.key, logger, lib_name=lib_name, skip_playlists=skip_playlists)
                if include_playlists else []
            )
            state.get_dashboard().advance_library(lib_name, 1)
            state.get_dashboard().set_library_phase(lib_name, "Reading collections…")

            collections = (
                [] if (skip_collections or not include_collections)
                else snapshot_collections(src_section, logger)
            )
            state.get_dashboard().advance_library(lib_name, 1)
            state.get_dashboard().set_library_phase(lib_name, "Reading ratings…")

            ratings = (
                snapshot_ratings(src_section, logger, user=source_owner)
                if include_ratings else []
            )
            state.get_dashboard().advance_library(lib_name, 1)
        else:
            # Owner skipped - empty library-level block, four phases
            # advanced together so the bar still reflects the four
            # owner phases as "done."
            watch_history = []
            playlists = []
            collections = []
            ratings = []
            state.get_dashboard().advance_library(lib_name, 4)
            state.get_dashboard().set_library_phase(lib_name, "Owner skipped (user-filter)")
            state.get_dashboard().push_activity(
                "phase", lib_name,
                "Owner data block skipped - user-filter excluded owner",
            )
    finally:
        state._plex_owner_name = saved_owner

    # ── Per-user gather ──────────────────────────────────────────────
    # Each transferable managed user reads their data on the source
    # via their own token-bound PlexServer connection. The data lands
    # in payload["users"][username] in the on-disk schema's shape so
    # restore_export_file's existing per-user import loop can replay
    # it under the matching destination user's token.
    #
    # Per-user collections need the owner's collection rating-key set
    # so library-level collections (visible to every
    # user via section.collections()) don't get double-counted into
    # every user's payload.
    owner_coll_keys_inmem: Set[Any] = {
        c.get("rating_key") for c in collections if c.get("rating_key") is not None
    }
    users_data = _gather_users_data(
        lib_name, source_home_users or [], owner_coll_keys_inmem, logger,
        skip_collections=skip_collections,
        fast_collection_detection=fast_collection_detection,
        skip_playlists=skip_playlists,
        # Per-user gather honours all four flags.
        include_watch_history=include_watch_history,
        include_ratings=include_ratings,
        include_playlists=include_playlists,
        include_collections=include_collections,
    )

    # ── Build the unified-users payload ──────────────────────────────
    # The owner is folded into ``users`` with ``role='owner'``; managed
    # users are ``role='managed'``. ``restore_export_file`` reads the
    # owner block via its role and the managed users via the same map.
    owner_display = source_owner or "Plex Owner"
    owner_json_key = owner_display
    _used = set(users_data.keys())
    _n = 2
    while owner_json_key in _used:
        owner_json_key = f"{owner_display} ({_n})"
        _n += 1
    unified_users: Dict[str, Any] = {
        owner_json_key: {
            "role": "owner",
            "display_name": owner_display,
            "backend_user_id": None,
            "watch_history": watch_history,
            "ratings":       ratings,
            "playlists":     playlists,
            "collections":   collections,
        },
    }
    for _h, _ub in users_data.items():
        if not isinstance(_ub, dict):
            continue
        unified_users[_h] = {
            "role": "managed",
            "display_name": _h,
            "backend_user_id": None,
            "watch_history": _ub.get("watch_history", []),
            "ratings":       _ub.get("ratings", []),
            "playlists":     _ub.get("playlists", []),
            "collections":   _ub.get("collections", []),
        }

    payload: Dict[str, Any] = {
        # ``library`` carries the SOURCE library title - matches what
        # the snapshotter writes for an on-disk export. The DB ingestion
        # below uses this name for the source-side media.db row, and the
        # source name is the right anchor for that table.
        # ``restore_export_file`` resolves the destination section via
        # the ``target_section_name_override`` kwarg we pass below
        # (which is the post-mapping destination name); this dual-name
        # split keeps the source-side cache labelled by source and the
        # destination-side write routed to the renamed library.
        "library": lib_name,
        # Library-section identity anchor. ``ingest_snapshot_payload``
        # asserts on these fields and refuses to write rows without them;
        # the snapshotter populates them on every per-library payload,
        # and direct-transfer must do the same so the source-side media.db
        # seeding below doesn't raise ValueError and silently skip the
        # cache write. ``src_section`` is the live Plex section object
        # (see the function signature); ``.key`` and ``.type`` are cached
        # attributes (no autoreload trip).
        "library_section_id": int(getattr(src_section, "key", 0) or 0),
        "library_section_type": str(getattr(src_section, "type", "") or ""),
        "captured_at": datetime.now().isoformat(),
        "snapshot_meta": {
            "server_name": getattr(source_server, "friendlyName", "") or "",
            "server_url": getattr(source_server, "_baseurl", "") or "",
            "server_machine_id": getattr(source_server, "machineIdentifier", "") or "",
            "server_version": getattr(source_server, "version", "unknown"),
            "backend": "plex",
            "trigger": "direct-transfer",
        },
        "users": unified_users,
        "stats": {
            "total_watched": len(watch_history),
            "total_playlists": len(playlists),
            "total_collections": len(collections),
            "total_rated": len(ratings),
            "total_home_users": len(users_data),
        },
    }

    # ── DB ingestion of the source-side payload ──────────────────────
    # We use the source server's Plex ``machineIdentifier`` as the
    # DB's ``server_id`` because (a) it's stable across registry
    # renames, (b) it's already on the PlexServer instance, and (c)
    # using it means the DB has zero coupling to the registry's UUIDs.
    # ``ingest_snapshot_payload`` enforces the server-wide vs user-private
    # dedup discipline so library-level collections / playlists land
    # exactly once even when N home users would otherwise duplicate
    # them. Best-effort - a DB hiccup must not break the transfer.
    try:
        from server import media_db
        from services.snapshot.plex_native.snapshotter import _should_cache_payload_to_media_db
        src_machine = str(getattr(source_server, "machineIdentifier", "") or "")
        if src_machine and _should_cache_payload_to_media_db(src_machine, lib_name, logger):
            counts = media_db.ingest_snapshot_payload(src_machine, payload)
            logger.debug(
                "[%s] DB ingest from source machine %r: %s",
                lib_name, src_machine, counts,
            )
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning(
            "[%s] DB ingestion of source payload failed (non-fatal): %s",
            lib_name, exc,
        )

    # ── Phase 2: merge into the destination ──────────────────────────
    # Widen the library's progress total from the placeholder 8 to the
    # real per-item count BEFORE handing off to ``restore_export_file``.
    # Each per-item ``_advance_lib`` call inside the import path then
    # contributes to a meaningful bar instead of saturating at 8 after
    # the first few items, which would make the top-level ETA climb
    # rather than count down during the import phase.
    #
    # Total = 4 (snapshot phases already completed, counted above) +
    # one tick per per-item or per-row operation the import side
    # will perform. This mirrors ``run_restore``'s own per-export-file
    # total computation in services/importer.py.
    import_total = _compute_import_total(payload, home_user_names={u[0] for u in (dest_home_users or [])})
    state.get_dashboard().set_library_total(
        lib_name,
        total=4 + import_total,
        completed=4,  # the four snapshot phases are done
    )

    state.get_dashboard().set_library_phase(lib_name, "Writing to destination…")
    # ``restore_export_file`` expects the dest connection's URL + token
    # in the module-level state (for /:/scrobble, /:/rate, /:/progress
    # direct HTTP calls). Save the previous values so we don't trample
    # them if a later phase needs them.
    prev_url, prev_tok = state._plex_base_url, state._plex_token
    state._plex_base_url = dest_url
    state._plex_token = dest_token
    state._plex_owner_name = "Plex Owner"  # dest owner not strictly needed; labels only
    try:
        sections_by_name = {s.title: s for s in dest_server.library.sections()}
        # Only prefetch the destination's playlist map when we
        # actually intend to import playlists AND the source-side
        # payload carries any. Without this gate, even
        # ``skip_playlists=True`` runs would pay a wasted
        # ``dest_server.playlists()`` round-trip per library.
        # Owner is just another user in the unified map, so one any()
        # walks both owner and managed-user blocks at once.
        payload_has_playlists = any(
            isinstance(u, dict) and bool(u.get("playlists"))
            for u in (payload.get("users", {}) or {}).values()
        )
        if include_playlists and payload_has_playlists:
            all_playlists = {pl.title: pl for pl in dest_server.playlists()}
        else:
            all_playlists = {}
        # ``restore_export_file`` expects a export_path string for log
        # messages but reads ``preloaded_data`` for the actual content.
        # The synthetic placeholder makes the run-log message explain
        # what happened ("direct://source/<library>") for forensic value.
        synthetic_path = f"direct://{lib_name}"
        # ``target_section_name_override`` is the post-mapping
        # destination library title. When the operator routed
        # ``Source Movies`` → ``Dest Movies (Renamed)`` via either a
        # per-run override or a saved mapping, the override here is the
        # renamed name and restore_export_file looks up the destination
        # section by that title rather than the source name embedded in
        # the payload. When the source and dest names match, this is a
        # no-op (override == payload['library']).
        restore_export_file(
            dest_server, synthetic_path, dest_token, dest_url,
            logger, log_dir, remap, strict_match,
            home_users=dest_home_users,
            existing_playlists=all_playlists,
            sections_by_name=sections_by_name,
            preloaded_data=payload,
            stop_event=stop_event,
            include_playlists=include_playlists,
            include_watch_history=include_watch_history,
            include_ratings=include_ratings,
            include_collections=include_collections,
            mode=mode,
            merge_watch_strategy=merge_watch_strategy,
            target_section_name_override=dest_lib_name,
            # Forward source/dest identity so the per-user fan-out's
            # identity_map resolver fires in cross-backend transfers.
            source_server_id=source_server_id,
            dest_server_id=dest_server_id,
        )
    finally:
        state._plex_base_url = prev_url
        state._plex_token = prev_tok

    # No post-advance here: the per-item advances inside
    # ``restore_export_file`` already filled the bar up to the
    # recomputed total. Anything else is a no-op via ``min(... ,
    # lib.total)`` clamping in advance_library.
    state.get_dashboard().push_activity("done", lib_name, "Direct transfer complete")


# ── Chained-snapshot fallback worker ──────────────────────────────────────────

def sweep_stale_tmp_exports(
    output_dir: str, max_age_seconds: float = 7 * 24 * 3600,
) -> int:
    """
    M2: remove ``*.tmp.plexexport.json`` files in ``output_dir`` older
    than ``max_age_seconds`` (default 7 days).

    These are chained-fallback payloads that a failed import left
    behind on purpose (see :func:`_chained_fallback_library`, Phase 4)
    so the end user can re-import them manually. Once they're old
    enough the end user has either recovered them or moved on, and
    they're sensitive (every user's watch history / ratings) - so we
    sweep them at startup. Returns the count removed. Best-effort:
    never raises.
    """
    removed = 0
    try:
        base = Path(output_dir)
        if not base.is_dir():
            return 0
        cutoff = time.time() - max_age_seconds
        for p in base.glob("*.tmp.plexexport.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception:
        log.exception("sweep_stale_tmp_exports failed for %r", output_dir)
    if removed:
        log.info(
            "Swept %d stale chained-transfer temp file(s) from %s",
            removed, output_dir,
        )
    return removed


def _chained_fallback_library(
    *,
    lib_name: str,
    src_section: Any,
    source_server: PlexServer,
    source_owner: str,
    dest_server: PlexServer,
    dest_url: str,
    dest_token: str,
    logger: logging.Logger,
    log_dir: str,
    remap: Optional[Tuple[str, str]],
    strict_match: bool,
    output_dir: str,
    stop_event: Optional[threading.Event] = None,
    source_home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    dest_home_users: Optional[List[Tuple[str, str, PlexServer]]] = None,
    owner_included: bool = True,
    skip_collections: bool = False,
    fast_collection_detection: bool = False,
    skip_playlists: bool = False,
    include_playlists: bool = True,
    # Additional include_* flags.
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_collections: bool = True,
    # Restore mode forwarded to restore_export_file at the end of the
    # chained-fallback flow.
    mode: str = "merge",
    merge_watch_strategy: str = "higher",
    # Per-library metric map. Forwarded to restore_export_file at the
    # end of the chained-fallback so the per-library choice applies on
    # the restore phase of the fallback.
    library_metrics: Optional[Dict[str, Dict[str, bool]]] = None,
    # Same forward as _transfer_one_library so the chained fallback
    # also honours the mapping table.
    ignore_library_mapping: bool = False,
    library_mapping_overrides: Optional[Dict[str, str]] = None,
    source_server_id: Optional[str] = None,
    dest_server_id: Optional[str] = None,
    # Destination library name (after mapping resolution at the caller
    # boundary). See ``_transfer_one_library`` for the rationale; same
    # default of ``lib_name`` so legacy direct callers keep working.
    dest_lib_name: Optional[str] = None,
) -> None:
    """
    Fallback path used by :func:`run_direct_transfer` when the
    in-memory direct attempt fails for one library.

    Flow:
      1. Gather data from the source the same way the direct path
         does (four export_* primitives, source-server connection).
      2. Write the data to a ``.tmp.plexexport.json`` file in
         ``output_dir`` - clearly marked as temporary so the user can
         identify and clean it up manually if this process is
         interrupted before the delete step.
      3. Hand that file to ``restore_export_file`` against the
         destination server.
      4. Delete the temp file on successful import.

    Errors during step 1 or 3 propagate, leaving the temp file on
    disk (per spec - the user should be able to recover it manually
    if needed). The activity feed receives a clear message at each
    step boundary so the user can tell where the process is.
    """
    # Match _transfer_one_library: default dest_lib_name to lib_name
    # when the caller didn't resolve a mapping. All source-side reads
    # use lib_name; all destination-side writes use dest_lib_name.
    if not dest_lib_name:
        dest_lib_name = lib_name
    # Reset progress for the chained path so the library bar runs
    # from 0 to 8 again - the user gets a visual restart that matches
    # the direct-then-chained retry sequence.
    state.get_dashboard().set_library_status(lib_name, "active")
    state.get_dashboard().set_library_phase(lib_name, "Chained: gathering from source…")

    # ── Phase 1: gather from source into a dict (same as direct) ──────
    # Gate the owner-side gather on the end user's filter choice,
    # identical to the in-memory path.
    saved_owner = state._plex_owner_name
    state._plex_owner_name = source_owner
    try:
        if owner_included:
            # Gate each owner-side gather on its include_* flag,
            # identical to the in-memory direct path.
            watch_history = (
                snapshot_watch_history(src_section, logger, user=source_owner)
                if include_watch_history else []
            )
            playlists = (
                snapshot_playlists(source_server, src_section.key, logger, lib_name=lib_name, skip_playlists=skip_playlists)
                if include_playlists else []
            )
            collections = (
                [] if (skip_collections or not include_collections)
                else snapshot_collections(src_section, logger)
            )
            ratings = (
                snapshot_ratings(src_section, logger, user=source_owner)
                if include_ratings else []
            )
        else:
            watch_history = []
            playlists = []
            collections = []
            ratings = []
            state.get_dashboard().push_activity(
                "phase", lib_name,
                "Owner data block skipped - user-filter excluded owner",
            )
    finally:
        state._plex_owner_name = saved_owner

    # Per-user gather mirrors the in-memory path so the chained
    # fallback produces an equivalent .plexexport.json - the
    # end user's filter choice is honoured no matter which route
    # the engine ends up taking for this library. Same owner-set
    # dedupe so library-level collections don't surface in every
    # user's personal block.
    owner_coll_keys_chained: Set[Any] = {
        c.get("rating_key") for c in collections if c.get("rating_key") is not None
    }
    users_data = _gather_users_data(
        lib_name, source_home_users or [], owner_coll_keys_chained, logger,
        skip_collections=skip_collections,
        fast_collection_detection=fast_collection_detection,
        skip_playlists=skip_playlists,
        # Pass include_* into the chained path's per-user gather so it
        # matches the in-memory path exactly.
        include_watch_history=include_watch_history,
        include_ratings=include_ratings,
        include_playlists=include_playlists,
        include_collections=include_collections,
    )

    # Unified-users payload (matches the in-memory path above).
    owner_display = source_owner or "Plex Owner"
    owner_json_key = owner_display
    _used = set(users_data.keys())
    _n = 2
    while owner_json_key in _used:
        owner_json_key = f"{owner_display} ({_n})"
        _n += 1
    unified_users: Dict[str, Any] = {
        owner_json_key: {
            "role": "owner",
            "display_name": owner_display,
            "backend_user_id": None,
            "watch_history": watch_history,
            "ratings":       ratings,
            "playlists":     playlists,
            "collections":   collections,
        },
    }
    for _h, _ub in users_data.items():
        if not isinstance(_ub, dict):
            continue
        unified_users[_h] = {
            "role": "managed",
            "display_name": _h,
            "backend_user_id": None,
            "watch_history": _ub.get("watch_history", []),
            "ratings":       _ub.get("ratings", []),
            "playlists":     _ub.get("playlists", []),
            "collections":   _ub.get("collections", []),
        }

    payload: Dict[str, Any] = {
        "library": lib_name,
        # Library-section identity anchor. See the matching block
        # in ``_transfer_one_library`` for the rationale; this chained-
        # fallback path must populate the same fields so the source-side
        # media.db seeding below succeeds.
        "library_section_id": int(getattr(src_section, "key", 0) or 0),
        "library_section_type": str(getattr(src_section, "type", "") or ""),
        "captured_at": datetime.now().isoformat(),
        "snapshot_meta": {
            "server_name": getattr(source_server, "friendlyName", "") or "",
            "server_url": getattr(source_server, "_baseurl", "") or "",
            "server_machine_id": getattr(source_server, "machineIdentifier", "") or "",
            "server_version": getattr(source_server, "version", "unknown"),
            "backend": "plex",
            "trigger": "direct-transfer",
        },
        "users": unified_users,
        "stats": {
            "total_watched": len(watch_history),
            "total_playlists": len(playlists),
            "total_collections": len(collections),
            "total_rated": len(ratings),
            "total_home_users": len(users_data),
        },
    }

    # DB ingestion mirrors the in-memory direct-transfer path. See
    # the longer note at the equivalent call site in
    # ``_transfer_one_library``. Identical contract so the chained
    # fallback doesn't leave a gap in the DB cache.
    try:
        from server import media_db
        from services.snapshot.plex_native.snapshotter import _should_cache_payload_to_media_db
        src_machine = str(getattr(source_server, "machineIdentifier", "") or "")
        if src_machine and _should_cache_payload_to_media_db(src_machine, lib_name, logger):
            counts = media_db.ingest_snapshot_payload(src_machine, payload)
            logger.debug(
                "[%s] DB ingest (chained fallback) from %r: %s",
                lib_name, src_machine, counts,
            )
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning(
            "[%s] DB ingestion (chained) failed (non-fatal): %s",
            lib_name, exc,
        )

    # ── Phase 2: write to disk with clear .tmp marker ─────────────────
    # ``state._run_timestamp`` already carries the "Source-to-Dest_<ts>"
    # slug, so the resulting filename looks like:
    #   Movies_Plex1-to-Plex2_20260511_021515.tmp.plexexport.json
    safe_lib_name = lib_name.replace(" ", "_").replace("/", "_")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = out_dir / f"{safe_lib_name}_{state._run_timestamp}.tmp.plexexport.json"
    state.get_dashboard().set_library_phase(lib_name, "Chained: writing temp file…")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    # M2: the payload holds every user's watch history, ratings, and
    # playlist membership for this library, and it may linger on disk
    # if the import below fails (Phase 4 deliberately leaves it for
    # manual recovery). Restrict it to owner-only. POSIX-only - on
    # Windows the data-directory ACL is the boundary (see README).
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    state.get_dashboard().push_activity(
        "phase", lib_name, f"Wrote temp file → {tmp_path.name}",
    )

    # ── Phase 3: import that file into the destination ────────────────
    # Same per-item total recompute as the in-memory path so the
    # chained fallback's progress bar behaves identically.
    import_total = _compute_import_total(payload, home_user_names={u[0] for u in (dest_home_users or [])})
    state.get_dashboard().set_library_total(
        lib_name,
        total=4 + import_total,
        completed=4,
    )

    state.get_dashboard().set_library_phase(lib_name, "Chained: importing into destination…")
    prev_url, prev_tok = state._plex_base_url, state._plex_token
    state._plex_base_url = dest_url
    state._plex_token = dest_token
    state._plex_owner_name = "Plex Owner"
    try:
        sections_by_name = {s.title: s for s in dest_server.library.sections()}
        # Mirror the in-memory path's gate. Only prefetch destination
        # playlists when we'll actually use them.
        # Unified users map - one any() covers owner + managed.
        payload_has_playlists = any(
            isinstance(u, dict) and bool(u.get("playlists"))
            for u in (payload.get("users", {}) or {}).values()
        )
        if include_playlists and payload_has_playlists:
            all_playlists = {pl.title: pl for pl in dest_server.playlists()}
        else:
            all_playlists = {}
        restore_export_file(
            dest_server, str(tmp_path), dest_token, dest_url,
            logger, log_dir, remap, strict_match,
            home_users=dest_home_users,
            existing_playlists=all_playlists,
            sections_by_name=sections_by_name,
            preloaded_data=payload,
            stop_event=stop_event,
            include_playlists=include_playlists,
            include_watch_history=include_watch_history,
            include_ratings=include_ratings,
            include_collections=include_collections,
            mode=mode,
            merge_watch_strategy=merge_watch_strategy,
            # Post-mapping destination library title. When the operator
            # routed source → renamed-dest, this override steers the
            # restore_export_file section lookup to the renamed library
            # instead of the source name in payload['library'].
            target_section_name_override=dest_lib_name,
            # Forward source/dest identity so the per-user fan-out's
            # identity_map resolver fires in cross-backend transfers.
            source_server_id=source_server_id,
            dest_server_id=dest_server_id,
        )
    except Exception as exc:
        # M2: the import failed, so Phase 4's unlink is never reached -
        # the temp file is deliberately left on disk for manual
        # re-import. Make sure the end user is explicitly told it
        # exists and where, rather than relying on them noticing the
        # ``.tmp`` marker. Re-raise so the job still fails, but with
        # the leftover path baked into the error the job record shows.
        logger.error(
            "Chained import for %r failed; the unimported payload was "
            "LEFT ON DISK for manual recovery at: %s  (it holds every "
            "user's watch history / ratings / playlist membership for "
            "this library - delete it once no longer needed). Cause: %s",
            lib_name, tmp_path, exc,
        )
        raise RuntimeError(
            f"Chained import for {lib_name!r} failed: {exc}. Unimported "
            f"payload left at {tmp_path} for manual re-import."
        ) from exc
    finally:
        state._plex_base_url = prev_url
        state._plex_token = prev_tok

    # ── Phase 4: delete the temp file only on success ─────────────────
    # We deliberately leave the file behind if anything in step 3
    # raised - the user can re-import it manually after fixing the
    # underlying issue. The .tmp marker in the filename makes it easy
    # to find and clean up if no longer needed.
    try:
        os.unlink(tmp_path)
        state.get_dashboard().push_activity(
            "phase", lib_name, "Chained: temp file deleted.",
        )
    except OSError as exc:
        # Not fatal - log and move on. The file just stays on disk.
        logger.warning(
            "Could not delete chained-transfer temp file %s: %s", tmp_path, exc,
        )

    state.get_dashboard().push_activity(
        "done", lib_name, "Chained snapshot-then-import complete.",
    )
