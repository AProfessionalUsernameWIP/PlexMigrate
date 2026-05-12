"""
plexmigrate.py — PlexMigrate (CLI driver).

This script safely moves your Plex Play Count, playlists, collections,
and star ratings from one Plex server to another. It exports your library
data to portable JSON backup files, then imports those files onto a new
server, matching each item using three identification methods in sequence.
All import operations are strictly additive — no existing data is ever
deleted, reduced, or overwritten on the target server.

v0.7.0 additions:
    - Per-user playlists are now exported and imported. Previously only the
      Plex owner's playlists were captured; home users' playlists were silently
      omitted from the backup. gather_user() now calls export_playlists() for
      each home user and stores the result under users[username]["playlists"].
      On import, _import_user() calls import_playlists() with the user's own
      server connection so playlists are created in the correct profile.
    - Home user import logging is substantially improved:
      · After get_home_users() in run_import(), the exact list of users
        available on the target server is logged before import starts.
      · Backup users are also logged so the admin can immediately see the
        match/miss without reading individual library logs.
      · "Home user not on target server" messages are now logged at INFO
        (not WARNING) — skipping is expected during a fresh migration and
        should not alarm the admin.
      · After the per-user import loop, a single summary line logs how many
        users were imported and lists which ones were skipped by name, giving
        a clear action list for a future re-run once those users are set up.
    - Progress bar totals now include per-user items for users that are
      available on the target server. Previously, if home user data was
      imported the bars overshot 100%. Totals are calculated only for users
      whose token was successfully fetched, so skipped users do not inflate
      the total and the bar does not stall short.

v0.6.1 additions:
    - Fixed --remap-path body separator bug: after swapping the root prefix,
      remaining backslashes in the path body are now normalised to forward slashes.
      A Windows-exported path A:\\Music\\Artist\\track.flac remapped to
      /mnt/music now correctly becomes /mnt/music/Music/Artist/track.flac
      instead of /mnt/music/Music\\Artist\\track.flac.
    - Fixed scan_cache and suffix index building for Music and TV libraries:
      section.all() returns Artist/Show objects (no file paths). The cache
      builder now calls section.searchTracks() for Music and
      section.searchEpisodes() for TV so leaf-level objects with real paths
      are indexed. Movies are unchanged (section.all() already returns Movie
      objects). This makes suffix matching functional for Music and TV.
    - Fixed scan_cache empty-library regression: the __suffix_index__ sentinel
      key was added unconditionally, causing bool(scan_cache) to be True even
      when no paths were indexed. The cache is now only marked as built when at
      least one real path was found, preserving the Plex API filter search
      fallback for empty or path-less libraries.

v0.6.0 additions:
    - Path-agnostic suffix matching (Tier 2.5) bridges cross-platform imports.
      When exporting from Windows and importing on Linux (or vice versa), the
      root path differs (C:\\Media vs /mnt/plex) but the tail is the same.
      A suffix index built alongside the filepath scan_cache lets resolve_item()
      look up the last 2–3 path components normalised to lowercase forward-slash
      in O(1) time. Unambiguous suffix hits resolve cleanly; ambiguous hits fall
      through to fuzzy search rather than guessing. Dashboard shows suffix hits
      separately from exact filepath hits. --remap-path and suffix matching
      complement each other.
    - Export activity feed and phase column now update during export: each gather
      closure (Watch History / Playlists / Collections / Ratings) pushes an
      activity event and phase label when it completes so the dashboard shows
      live progress instead of freezing on "Export started".
    - [S] keyboard shortcut now opens the Plex web UI pre-authenticated using
      the stored token (/web/index.html?X-Plex-Token=…). Falls back to the
      bare server URL if the token is unavailable.
    - Log lines above the dashboard always show the attributed user (including
      the Plex owner by their actual myPlexUsername, not "Plex Owner").

v0.5.0 additions:
    - Full htop-style terminal dashboard replaces the simple progress bars.
      On terminals ≥ 80×22, a structured panel shows a thread pool summary,
      run stats, per-library progress bars with ETA, a live activity feed
      (last 8 actions, colour-coded by type), and match resolution counters.
    - Keyboard shortcuts: Q=quit, V=toggle verbose, P=pause/resume workers,
      L=open log folder in the OS file manager.
    - Small-terminal fallback (< 80×22) retains the v0.4.0 Rich Progress bars.
    - DashboardState class is the single source of truth; all worker threads
      write to it under a lock; the display loop reads snapshots at 4 Hz.
    - Resolution tracking: each resolved item increments the GUID / filepath /
      fuzzy / unresolved counter so the dashboard shows match quality live.
    - Activity feed populated by _record_success() and _record_failure() so
      meaningful events appear even in screen=True (full-screen) mode.

v0.4.0 additions:
    - Rich Live panel with RichHandler replaces tqdm — log lines scroll above
      the panel without terminal corruption. Per-library phase labels update
      as each import moves through Play Count → Playlists → Collections →
      Ratings. Transient HTTP errors auto-retry (up to 2×). Home user token
      fetching is parallelised. TV export uses a server-side watched-only
      filter. Filepath scan cache is pre-warmed in a background thread.

Usage:
    python plexmigrate.py [flags]
    See README.md for full instructions and examples.

Dependencies:
    plexapi>=4.15, rich, requests
    Install with: pip install plexapi rich requests

Author note:
    This file is the entry-point driver. All logic lives in services/.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from rich.prompt import Prompt

import services.state as state
from services.state import VERSION, PLEX_PORT, DEFAULT_OUTPUT_DIR, DEFAULT_LOG_DIR, console
from services.auth import (
    connect_to_server,
    discover_libraries,
    display_discovery,
    find_preferences_xml,
    read_token_from_prefs,
)
from services.auth import _make_session
from services.exporter import run_export
from services.importer import run_import
from services.logging_ops import _tz_now, setup_logging


# ── Interactive Prompts ───────────────────────────────────────────────────────

def prompt_mode() -> str:
    """
    Asks the user to choose between export and import mode.

    Returns:
        "E" for export or "I" for import.
    """
    console.print("[bold]Mode?[/bold] [[cyan]E[/cyan]]xport / [[cyan]I[/cyan]]mport")
    choice = Prompt.ask("Enter choice", choices=["E", "e", "I", "i"])
    return choice.upper()


def prompt_library_selection(libs: List[dict]) -> List[str]:
    """
    Prompts the user to select which libraries to export or import.

    Args:
        libs (List[Dict]): Library list from discover_libraries().

    Returns:
        List of library name strings the user selected.
    """
    console.print("\n[bold]Available libraries:[/bold]")
    for i, lib in enumerate(libs, 1):
        console.print(f"  [{i}] {lib['name']} ({lib['type']}, {lib['count']} items)")
    console.print("  [A] All libraries")

    raw = Prompt.ask("\nEnter library numbers separated by commas, or A for all")

    if raw.strip().upper() == "A":
        return [lib["name"] for lib in libs]

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(libs):
                selected.append(libs[idx]["name"])
    return selected


def prompt_backup_files() -> List[str]:
    """
    Prompts the user to enter paths to .plexbackup.json files for import.

    Returns:
        List of file path strings.
    """
    console.print("\n[bold]Enter .plexbackup.json file paths[/bold] (comma-separated):")
    raw = Prompt.ask("Files")
    return [p.strip() for p in raw.split(",") if p.strip()]


# ── CLI Argument Parsing ──────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """
    Builds and returns the argument parser for the command-line interface.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="plexmigrate",
        description=(
            "Migrate Plex Play Count, playlists, collections, and ratings "
            "across servers. All import operations are strictly additive — "
            "no data on the target server is ever deleted or reduced."
        ),
    )

    parser.add_argument("--export", action="store_true",
                        help="Run in export mode (save data from this server)")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="Run in import mode (restore data to a server)")

    parser.add_argument("--token", metavar="TOKEN",
                        help="Plex authentication token (auto-read from Preferences.xml if omitted)")
    parser.add_argument(
        "--server", metavar="URL",
        default=f"http://localhost:{PLEX_PORT}",
        help=f"Plex server URL (default: http://localhost:{PLEX_PORT})",
    )

    parser.add_argument("--output-dir", metavar="PATH", default=DEFAULT_OUTPUT_DIR,
                        help=f"Directory for export files (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--libraries", metavar="NAMES",
                        help='Comma-separated library names to export, e.g. "Movies,TV Shows,Music"')

    parser.add_argument("--input-file", metavar="FILE", nargs="+",
                        help="One or more .plexbackup.json files to import")
    parser.add_argument(
        "--overwrite-playlists", action="store_true",
        help=(
            "Accepted for backward compatibility. In v0.2.0+, all playlist imports "
            "use additive union merge regardless of this flag — no deletions occur."
        ),
    )
    parser.add_argument("--remap-path", metavar=("OLD", "NEW"), nargs=2,
                        help="Translate file paths: replace OLD root prefix with NEW during import")

    parser.add_argument(
        "--strict-match", action="store_true", default=True,
        help="Require exactly one fuzzy title match (default: on)",
    )
    parser.add_argument(
        "--no-strict-match", dest="strict_match", action="store_false",
        help="Use first result when multiple fuzzy title matches exist (use carefully)",
    )

    # ── Multi-server registry (v0.9.0) ──────────────────────────────────────
    # These flags let CLI users manage the same servers.json file the
    # web UI reads from. Storage lives in $PLEXMIGRATE_DATA_DIR (default:
    # ./server_data/), so a CLI add and a web add show up in each
    # other's lists without re-syncing.
    parser.add_argument("--list-servers", action="store_true",
                        help="List every registered Plex server and exit.")
    parser.add_argument("--add-server", metavar="NAME",
                        help="Register a new server with this friendly NAME. Combine with --server URL --token TOK.")
    parser.add_argument("--remove-server", metavar="NAME",
                        help="Remove the server with this friendly NAME from the registry (does not delete exports or logs).")
    parser.add_argument("--rename-server", metavar=("OLD", "NEW"), nargs=2,
                        help="Rename an existing registered server.")
    parser.add_argument("--test-server", metavar="NAME",
                        help="Probe a registered server's connection and exit.")

    # ── Multi-server operation flags ────────────────────────────────────────
    parser.add_argument("--source-server", metavar="NAME",
                        help="Friendly name of a registered server to read from (export or direct transfer).")
    parser.add_argument("--dest-server", metavar="NAME",
                        help="Friendly name of a registered server to write to (import or direct transfer).")
    parser.add_argument("--direct", action="store_true",
                        help="Run a direct server-to-server transfer. Requires --source-server and --dest-server.")

    parser.add_argument("--workers", metavar="N", type=int, default=state.MAX_WORKERS,
                        help=f"Number of parallel worker threads for resolution (default: {state.MAX_WORKERS})")
    parser.add_argument(
        "--scrobble-workers", metavar="N", type=int, default=state.SCROBBLE_WORKERS,
        help=(
            f"Max simultaneous scrobble (view-count write) calls (default: {state.SCROBBLE_WORKERS}). "
            f"Lower this on a NAS or VM to keep Plex responsive during import."
        ),
    )
    parser.add_argument("--verbose", action="store_true",
                        help="Enable DEBUG-level logging to console and run log")
    parser.add_argument("--log-dir", metavar="PATH", default=DEFAULT_LOG_DIR,
                        help=f"Directory for log files (default: {DEFAULT_LOG_DIR})")

    return parser


# ── Main Entry Point ──────────────────────────────────────────────────────────

def _run_cli_export_or_import(
    args: argparse.Namespace,
    server: object,
    base_url: str,
    token: str,
    logger: logging.Logger,
    run_log_dir: str,
    libs: List[dict],
) -> None:
    """
    Original export / import dispatch, factored out of ``main`` so the
    multi-server (--source-server) and legacy ad-hoc (--server + --token)
    paths share one implementation. Behaviour is byte-for-byte identical
    to v0.7.1 — only the *connection setup* changed.
    """
    remap: Optional[Tuple[str, str]] = tuple(args.remap_path) if args.remap_path else None

    if args.export:
        mode = "E"
    elif args.do_import:
        mode = "I"
    else:
        mode = prompt_mode()

    if mode == "E":
        if args.libraries:
            selected_names = [n.strip() for n in args.libraries.split(",")]
        else:
            selected_names = prompt_library_selection(libs)

        selected_sections = [
            sec for sec in server.library.sections()  # type: ignore[attr-defined]
            if sec.title in selected_names
        ]

        if not selected_sections:
            console.print("[red]No matching libraries found. Check the library names and try again.[/red]")
            sys.exit(1)

        run_export(server, selected_sections, args.output_dir, logger, run_log_dir, base_url)

    else:
        if args.input_file:
            backup_files = args.input_file
        else:
            backup_files = prompt_backup_files()

        valid_files = [f for f in backup_files if Path(f).exists()]
        missing = set(backup_files) - set(valid_files)
        for mf in missing:
            logger.error(f"Backup file not found: {mf}")

        if not valid_files:
            console.print("[red]No valid backup files found. Check the file paths and try again.[/red]")
            sys.exit(1)

        run_import(
            server, valid_files, token, base_url,
            logger, run_log_dir, remap, args.strict_match,
        )


def _run_cli_direct(args: argparse.Namespace, logger: logging.Logger, run_log_dir: str) -> None:
    """
    CLI driver for ``--direct --source-server NAME1 --dest-server NAME2``.

    Resolves both registered servers, prefixes ``_run_timestamp`` with
    the combined slug ("Src-to-Dst"), and hands off to
    :func:`server.direct_transfer.run_direct_transfer`. The engine
    code itself is unchanged — direct transfer is a new orchestrator
    on top of the existing export_* and import_* primitives.
    """
    from datetime import datetime as _dt
    from server import server_registry
    from server.direct_transfer import run_direct_transfer

    try:
        src_server, src_row = server_registry.connect_registered_server(args.source_server, logger)
        dst_server, dst_row = server_registry.connect_registered_server(args.dest_server, logger)
    except (ValueError, ConnectionError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    if src_row["name"] == dst_row["name"]:
        console.print("[red]Source and destination must be different registered servers.[/red]")
        sys.exit(2)

    # Prefix the run log dir with both server slugs so the artefact
    # name on disk records the direction of the transfer.
    src_slug = server_registry.safe_server_name(src_row["name"])
    dst_slug = server_registry.safe_server_name(dst_row["name"])
    combined = f"{src_slug}-to-{dst_slug}"
    state._run_timestamp = f"{combined}_{_dt.now().strftime('%Y%m%d_%H%M%S')}"

    logger.info(
        f"Direct transfer: {src_row['name']!r} ({src_row['url']}) "
        f"→ {dst_row['name']!r} ({dst_row['url']})"
    )

    libraries: List[str] = []
    if args.libraries:
        libraries = [n.strip() for n in args.libraries.split(",") if n.strip()]

    remap: Optional[Tuple[str, str]] = tuple(args.remap_path) if args.remap_path else None

    run_direct_transfer(
        source_server=src_server,
        source_url=src_row["url"],
        source_token=src_row["token"],
        source_owner=src_row.get("owner_name") or "Plex Owner",
        dest_server=dst_server,
        dest_url=dst_row["url"],
        dest_token=dst_row["token"],
        dest_owner=dst_row.get("owner_name") or "Plex Owner",
        library_names=libraries,
        logger=logger,
        log_dir=run_log_dir,
        remap=remap,
        strict_match=args.strict_match,
        stop_event=None,
    )
    logger.info(f"PlexMigrate v{VERSION} direct transfer finished — {_tz_now()}")
    console.print("[bold]Direct transfer complete.[/bold]")


def _handle_registry_commands(args: argparse.Namespace) -> bool:
    """
    Run any of the multi-server registry management commands.

    Returns True if a registry command was handled (caller should exit
    cleanly), False otherwise so the regular export/import flow runs.

    These commands intentionally do NOT call setup_logging() — they
    just print to stdout. The registry file is the only artefact and
    a per-run log directory would be noise.
    """
    # Import locally so the engine-only install (which doesn't pull the
    # FastAPI/Pydantic dependency) doesn't import server-side packages
    # unless a server-related flag is actually used.
    from server import server_registry

    if args.list_servers:
        rows = server_registry.list_servers(include_tokens=False)
        if not rows:
            console.print("[dim]No servers registered. Add one with: "
                          "--add-server NAME --server URL --token TOK[/dim]")
            return True
        console.print("[bold]Registered Plex servers:[/bold]")
        for row in rows:
            status = row.get("last_status") or "unknown"
            colour = {"ok": "green", "unreachable": "red", "auth_error": "red"}.get(status, "yellow")
            console.print(
                f"  • [{colour}]{status:11s}[/{colour}]  "
                f"{row['name']:24s}  {row.get('url','')}"
            )
        return True

    if args.add_server:
        if not args.server or not args.token:
            console.print("[red]--add-server requires --server URL and --token TOKEN.[/red]")
            sys.exit(2)
        try:
            row = server_registry.add_server(args.add_server, args.server, args.token)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(2)
        console.print(f"[green]Registered {row['name']!r} → {row['url']}[/green]")
        # Best-effort connection check so the user sees immediately if
        # the token is wrong.
        import logging as _logging
        boot = _logging.getLogger("plexmigrate")
        if not boot.handlers:
            boot.addHandler(_logging.StreamHandler())
        try:
            server_registry.test_connection(row["id"], boot)
            console.print("[dim]Connection check OK.[/dim]")
        except Exception as e:
            console.print(f"[yellow]Connection check failed: {e}[/yellow]")
        return True

    if args.remove_server:
        summary = server_registry.remove_server_by_name(args.remove_server)
        if summary is None:
            console.print(f"[red]No server named {args.remove_server!r} in the registry.[/red]")
            sys.exit(2)
        # v0.9.5: remove is now a cascading delete — drops the registry
        # row, schedules referencing the server, and any
        # ``.plexbackup.json`` / ``run_<slug>_*`` artefacts attributable
        # to it. Report the counts so the operator sees what happened.
        console.print(f"[green]Removed {args.remove_server!r} from the registry.[/green]")
        console.print(
            f"[dim]Cascade: {summary['schedules']} schedule(s), "
            f"{summary['exports']} export file(s), "
            f"{summary['log_dirs']} log directory/ies deleted.[/dim]"
        )
        if summary.get("errors"):
            console.print("[yellow]Some items could not be removed:[/yellow]")
            for err in summary["errors"]:
                console.print(f"  [dim]{err}[/dim]")
        return True

    if args.rename_server:
        old, new = args.rename_server
        row = server_registry.get_server_by_name(old)
        if row is None:
            console.print(f"[red]No server named {old!r}.[/red]")
            sys.exit(2)
        try:
            server_registry.update_server(row["id"], name=new)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(2)
        console.print(f"[green]Renamed {old!r} → {new!r}.[/green]")
        return True

    if args.test_server:
        row = server_registry.get_server_by_name(args.test_server)
        if row is None:
            console.print(f"[red]No server named {args.test_server!r}.[/red]")
            sys.exit(2)
        import logging as _logging
        boot = _logging.getLogger("plexmigrate")
        if not boot.handlers:
            boot.addHandler(_logging.StreamHandler())
        after = server_registry.test_connection(row["id"], boot)
        status = after.get("last_status") or "unknown"
        detail = after.get("last_status_detail") or ""
        console.print(f"[bold]{after['name']}[/bold] → {status}")
        if detail:
            console.print(f"  {detail}")
        return True

    return False


def main() -> None:
    """
    Main entry point: parses arguments, connects to Plex, and runs export or import.
    """
    parser = build_parser()
    args = parser.parse_args()

    # ── Multi-server registry commands take precedence ──────────────────
    # If any of --list-servers / --add-server / --remove-server /
    # --rename-server / --test-server is present, run that and exit
    # without touching the export/import pipeline.
    if any([args.list_servers, args.add_server, args.remove_server,
            args.rename_server, args.test_server]):
        if _handle_registry_commands(args):
            return

    state.MAX_WORKERS = args.workers
    state.SCROBBLE_WORKERS = args.scrobble_workers
    state._session = _make_session()

    logger = setup_logging(args.log_dir, args.verbose)
    run_log_dir = str(state._run_log_dir)
    console.print(f"[dim]Logs: {state._run_log_dir}[/dim]")
    logger.info(f"PlexMigrate v{VERSION} starting — {_tz_now()}")

    # ── Direct server-to-server transfer (v0.9.0) ──────────────────────
    # Requires --source-server and --dest-server. Both must be in the
    # registry. Reads from source and writes to dest with no
    # intermediate file. Library list defaults to "everything on both
    # servers" when --libraries is omitted.
    if args.direct:
        if not args.source_server or not args.dest_server:
            console.print("[red]--direct requires --source-server NAME and --dest-server NAME.[/red]")
            sys.exit(2)
        _run_cli_direct(args, logger, run_log_dir)
        return

    # ── Multi-server export ────────────────────────────────────────────
    # If --source-server is supplied, resolve URL+token via the registry
    # and ignore --server / --token. Otherwise the legacy ad-hoc path runs.
    token: Optional[str] = args.token
    base_url = args.server
    if args.source_server:
        from server import server_registry
        try:
            srv_obj, srv_row = server_registry.connect_registered_server(args.source_server, logger)
        except (ValueError, ConnectionError) as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        token = srv_row["token"]
        base_url = srv_row["url"]
        server = srv_obj
        state._plex_base_url = base_url
        state._plex_token = token
        state._plex_owner_name = srv_row.get("owner_name") or "Plex Owner"
        # Prefix log dir + export filenames with server slug for parity
        # with how the FastAPI job runner names artefacts.
        from datetime import datetime as _dt
        slug = server_registry.safe_server_name(srv_row["name"])
        state._run_timestamp = f"{slug}_{_dt.now().strftime('%Y%m%d_%H%M%S')}"
        logger.info(f"Using registered server {srv_row['name']!r} ({base_url})")
        libs = discover_libraries(server, logger)
        display_discovery(server, libs)
        _run_cli_export_or_import(args, server, base_url, token, logger, run_log_dir, libs)
        return

    if not token:
        prefs = find_preferences_xml()
        if prefs:
            token = read_token_from_prefs(prefs)
            if token:
                logger.info(f"Auto-discovered Plex token from {prefs}")
            else:
                logger.warning("Found Preferences.xml but PlexOnlineToken attribute is missing.")

        if not token:
            console.print("[yellow]Plex token not found automatically.[/yellow]")
            token = Prompt.ask("Enter your Plex token")

    server = connect_to_server(args.server, token, logger)
    state._plex_base_url = args.server
    state._plex_token = token
    if server:
        state._plex_owner_name = getattr(server, "myPlexUsername", None) or "Plex Owner"
        logger.info(f"Connected as: {state._plex_owner_name}")
    if not server:
        console.print(
            f"[red]Cannot connect to Plex server at {args.server}. "
            f"Check that Plex is running and your token is correct.[/red]"
        )
        sys.exit(1)

    libs = discover_libraries(server, logger)
    display_discovery(server, libs)

    _run_cli_export_or_import(args, server, args.server, token, logger, run_log_dir, libs)
    logger.info(f"PlexMigrate v{VERSION} finished — {_tz_now()}")
    console.print("[bold]Done.[/bold]")

    for lg in (logging.getLogger("plexmigrate"), logging.getLogger("plexmigrate.media")):
        for h in lg.handlers[:]:
            h.close()
            lg.removeHandler(h)

    if state._run_log_dir and state._run_log_dir.exists():
        errors_file = state._run_log_dir / "errors.log"
        passed = not (errors_file.exists() and errors_file.stat().st_size > 0)
        suffix = "PASS" if passed else "FAIL"
        final_dir = state._run_log_dir.parent / f"{state._run_log_dir.name}_{suffix}"
        try:
            state._run_log_dir.rename(final_dir)
            console.print(f"[dim]Logs saved → {final_dir}[/dim]")
        except Exception:
            console.print(f"[dim]Logs saved → {state._run_log_dir}[/dim]")


if __name__ == "__main__":
    main()
