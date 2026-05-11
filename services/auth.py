"""
Plex server authentication and library discovery for PlexMigrate.

Contains token auto-discovery, server connection, library enumeration,
home user token fetching, and the shared HTTP session / retry adapter factory.
"""

import concurrent.futures
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from plexapi.server import PlexServer
from requests.adapters import HTTPAdapter
from rich.table import Table
from urllib3.util.retry import Retry

import services.state as state
from services.state import PLEX_DB_PATHS, console


# ── HTTP Session / Retry Adapter ──────────────────────────────────────────────

def _make_retry_adapter(pool_maxsize: int = 10) -> HTTPAdapter:
    """
    Builds an HTTPAdapter with a consistent retry policy.

    Both our own _session and plexapi's internal server._session need identical
    retry behaviour. Extracting the construction here means the policy is defined
    once and applied to both sessions.

    Args:
        pool_maxsize (int): Max simultaneous open connections in the pool.
    """
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "PUT"]),
        raise_on_status=False,
    )
    return HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=pool_maxsize)


def _make_session() -> requests.Session:
    """
    Builds and returns a requests.Session with the shared retry adapter.

    Centralises session creation so _scrobble, _rate_item, and
    _set_resume_position all share one TCP connection pool and one
    retry policy without per-call setup.
    """
    session = requests.Session()
    adapter = _make_retry_adapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# ── Plex Server Discovery ─────────────────────────────────────────────────────

def find_preferences_xml() -> Optional[Path]:
    """
    Scans OS-specific paths for Plex's Preferences.xml configuration file.

    By finding and reading this file, we can connect to Plex without asking
    the user to look up their token manually.

    Returns:
        Path to Preferences.xml if found, or None if not found anywhere.
    """
    platform = sys.platform
    paths = PLEX_DB_PATHS.get(platform, PLEX_DB_PATHS.get("linux", []))

    for base in paths:
        candidate = Path(base) / "Preferences.xml"
        if candidate.exists():
            return candidate

    return None


def read_token_from_prefs(prefs_path: Path) -> Optional[str]:
    """
    Reads the PlexOnlineToken attribute from a Preferences.xml file.

    Args:
        prefs_path (Path): Full path to Preferences.xml.

    Returns:
        The token string if found and non-empty, or None.
    """
    try:
        tree = ET.parse(prefs_path)
        root = tree.getroot()
        token = root.get("PlexOnlineToken")
        return token if token else None
    except Exception:
        return None


def connect_to_server(url: str, token: str, logger: logging.Logger) -> Optional[PlexServer]:
    """
    Connects to a Plex Media Server and returns the connection object.

    Also mounts a retry adapter onto plexapi's internal session so GUID
    lookups, section.search(), and section.all() benefit from the same
    retry policy as our own HTTP calls.

    Args:
        url (str): Full server URL including protocol and port.
        token (str): Plex authentication token.
        logger (Logger): Shared logger for recording the outcome.

    Returns:
        PlexServer if connection succeeds, or None if it fails.
    """
    try:
        server = PlexServer(url, token, timeout=120)

        if hasattr(server, "_session"):
            adapter = _make_retry_adapter(pool_maxsize=min(state.MAX_WORKERS, 16))
            server._session.mount("http://", adapter)
            server._session.mount("https://", adapter)

        logger.info(
            f"Connected to Plex server: {server.friendlyName} (version {server.version})"
        )
        return server

    except Exception as e:
        logger.error(f"Failed to connect to Plex server at {url}: {e}")
        return None


def discover_libraries(server: PlexServer, logger: logging.Logger) -> List[Dict]:
    """
    Returns a summary list of all libraries on the connected Plex server.

    Args:
        server (PlexServer): Active connection to the Plex server.
        logger (Logger): Shared logger.

    Returns:
        List of dicts, each with keys: name, type, key, count.
    """
    libs = []

    for section in server.library.sections():
        try:
            count = section.totalSize
        except Exception:
            count = len(section.all())

        libs.append({
            "name": section.title,
            "type": section.type,
            "key": section.key,
            "count": count,
        })
        logger.debug(f"Discovered library: {section.title} ({section.type}, {count} items)")

    return libs


def display_discovery(server: PlexServer, libs: List[Dict]) -> None:
    """
    Prints a formatted discovery summary table to the terminal.

    Args:
        server (PlexServer): Active server connection (used for name/version).
        libs (List[Dict]): Library list from discover_libraries().
    """
    console.print(
        f"\n[bold green]Connected:[/bold green] "
        f"{server.friendlyName} — Plex {server.version}\n"
    )

    table = Table(title="Libraries Found", show_header=True, header_style="bold cyan")
    table.add_column("Library", style="white")
    table.add_column("Type", style="dim")
    table.add_column("Items", justify="right")

    for lib in libs:
        table.add_row(lib["name"], lib["type"], str(lib["count"]))

    console.print(table)
    console.print()


# ── Multi-User Support ────────────────────────────────────────────────────────

def get_home_users(
    server: PlexServer,
    base_url: str,
    logger: logging.Logger,
) -> List[Tuple[str, str, PlexServer]]:
    """
    Returns a list of (username, token, server) for each Plex Home managed user.

    Plex Home lets multiple profiles share one server. Each profile has its own
    independent Play Count and star ratings — the admin account's data does not
    include managed users' data. This function authenticates as each managed user
    so their data can be exported and imported separately.

    Args:
        server (PlexServer): Admin server connection (must be linked to Plex.tv).
        base_url (str): Plex server base URL, e.g. "http://localhost:32400".
        logger (Logger): Shared logger.

    Returns:
        List of (username, user_token, user_server) tuples — one per managed user.
        Returns an empty list if the server uses a LocalAdminToken, if there are
        no managed users, or if an error occurs.
    """
    result: List[Tuple[str, str, PlexServer]] = []
    try:
        account = server.myPlexAccount()
        users = account.users()

        if not users:
            logger.info("No Plex Home managed users found on this account.")
            return result

        # Surface the slow per-user auth burst on the dashboard's
        # activity feed — without this the user sees nothing for the
        # 5-30 s it can take to walk every home user, especially when
        # some 401 and trigger plexapi's retry backoff.
        if state._dashboard:
            state._dashboard.push_activity(
                "phase", "—", f"Authenticating {len(users)} home user(s)…",
            )

        # ── Fetch per-user tokens in parallel ─────────────────────────────────
        def _connect_user(user):
            user_token = user.get_token(server.machineIdentifier)
            user_server = PlexServer(base_url, user_token, timeout=120)
            return user.title, user_token, user_server

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(users)) as pool:
            futs = {pool.submit(_connect_user, u): u for u in users}
            for fut in concurrent.futures.as_completed(futs):
                u = futs[fut]
                try:
                    title, user_token, user_server = fut.result()
                    result.append((title, user_token, user_server))
                    logger.info(f"Connected as home user: {title}")
                except Exception as e:
                    logger.warning(f"Could not connect as home user '{u.title}': {e}")

        if state._dashboard:
            state._dashboard.push_activity(
                "started", "—",
                f"Home users ready: {len(result)} of {len(users)} authenticated",
            )

    except Exception as e:
        logger.info(
            f"Multi-user support unavailable (requires a Plex.tv-linked account): {e}. "
            f"Only admin account data will be processed."
        )

    return result
