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

    v0.9.6: 429 is now in ``status_forcelist`` with
    ``respect_retry_after_header=True``, and the total budget is bumped
    from 2 to 4 so the engine absorbs Plex throttling rather than
    surfacing 429s to callers. ``connect`` and ``read`` budgets are
    unchanged.

    Args:
        pool_maxsize (int): Max simultaneous open connections in the pool.
    """
    retry = Retry(
        total=4,
        connect=2,
        read=1,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "PUT"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    return HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=pool_maxsize)


def _http_response_hook(response, *args, **kwargs):
    """
    Per-response hook installed on every Plex session (v0.9.6 Feature 2).

    Records status code + latency into ``state._dashboard`` so the
    Network panel can render the histogram, the rolling rate / latency
    graph, and the rate-limit feed. Best-effort: any error inside the
    hook is swallowed so a telemetry hiccup never breaks a real HTTP
    call.

    The library attribution comes from
    :data:`services.dashboard._http_lib_var` - a ContextVar that the
    per-library task entry sets and ``submit_with_context`` propagates
    into nested worker threads.
    """
    try:
        # Diagnostic: count every Plex HTTP response against the
        # calling thread so the snapshotter can detect per-item
        # reload N+1s in its serialize loops.
        state.bump_http_count()

        # Lazy import keeps this module importable even on a host
        # without the dashboard module loaded (CLI-only checkouts).
        from services.dashboard import _http_lib_var

        # Lookup library context; default to empty string → "__all__"
        # bucket in record_http_response.
        try:
            library = _http_lib_var.get()
        except Exception:
            library = ""

        # ``response.elapsed`` is a timedelta; convert to ms.
        try:
            elapsed_ms = response.elapsed.total_seconds() * 1000.0
        except Exception:
            elapsed_ms = 0.0

        # Read Retry-After when the server is throttling us. Plex
        # sometimes returns it as a delta-seconds integer and sometimes
        # omits it; the dashboard tolerates None.
        retry_after: Optional[float] = None
        if response.status_code == 429:
            ra_raw = response.headers.get("Retry-After")
            if ra_raw:
                try:
                    retry_after = float(ra_raw)
                except (TypeError, ValueError):
                    retry_after = None

        # urllib3 stuffs the retry-attempt history on response.raw -
        # one entry per retry actually performed. Count them once per
        # final response so the cumulative retry counter reflects work
        # that's already done, not pending retries.
        try:
            history = getattr(response.raw, "retries", None)
            history_list = getattr(history, "history", None)
            if history_list:
                state.get_dashboard().inc_http_retry(len(history_list))
        except Exception:
            pass

        if state.get_dashboard() is not None:
            state.get_dashboard().record_http_response(
                library=library,
                status_code=int(response.status_code),
                elapsed_ms=elapsed_ms,
                retry_after_seconds=retry_after,
            )

        # v0.12.0 - feed the process-lifetime, server-keyed collector
        # so the Networking tab has data even when no job is running
        # and so fan-out destinations are individually visible there.
        # Lazy-import to keep this module loadable in CLI-only checkouts
        # that don't pull in the server package.
        try:
            from server import network_collector as _nc
            _nc.record_response(
                url=str(response.url or ""),
                status_code=int(response.status_code),
                elapsed_ms=elapsed_ms,
                retry_after_seconds=retry_after,
            )
        except Exception:
            pass
    except Exception:
        # Telemetry must never break the response path.
        pass


def _install_response_hook(session: requests.Session) -> None:
    """
    Attach :func:`_http_response_hook` to ``session.hooks["response"]``
    if it isn't already present. Idempotent so re-mounting an adapter
    later (e.g. plexapi's session getting a fresh adapter) doesn't
    register the hook twice.
    """
    existing = session.hooks.setdefault("response", [])
    # ``hooks["response"]`` accepts either a single callable or a list;
    # normalise to a list so we can dedupe.
    if not isinstance(existing, list):
        existing = [existing] if existing else []
        session.hooks["response"] = existing
    if _http_response_hook not in existing:
        existing.append(_http_response_hook)


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
    _install_response_hook(session)
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
            # v0.9.6: every Plex API call plexapi makes flows through
            # this session - section.search, getByGuid, playlists,
            # systemAccounts, etc. Installing the telemetry hook here
            # captures the bulk of the engine's HTTP traffic so the
            # Network panel doesn't miss it.
            _install_response_hook(server._session)

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
        f"{server.friendlyName} - Plex {server.version}\n"
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

def _lookup_stored_pin(username: str, machine_identifier: str) -> Optional[str]:
    """
    PR-13 fix #4 helper. Resolve the stored Plex Home PIN for a
    managed user via the PR-10 ``managed_users`` table, looking the
    server up by ``machine_identifier`` (which the engine has handy
    via ``PlexServer.machineIdentifier``) rather than friendly name.

    Returns the decrypted PIN (plaintext) or ``None`` if no PIN is
    stored or the server isn't registered. Best-effort: any error
    decoding the ciphertext returns ``None`` so the caller falls
    back to its no-PIN path rather than crashing the per-user loop.

    PR-13 audit follow-up: every PIN read is recorded in
    ``db_access.log`` so the operator can confirm the stored-PIN
    path is actually firing for a given user.
    """
    if not username or not machine_identifier:
        return None
    try:
        # Late import: this module is in ``services/`` (engine layer)
        # and the registry / DB modules are in ``server/``. Top-level
        # imports here would create a layering dependency between
        # the engine and the FastAPI server. Late-binding keeps the
        # CLI usable even when the server package is partially
        # importable.
        from server import server_registry, media_db
        from services import db_access_log
    except Exception:
        return None
    try:
        # Walk the registry for a row whose machine_identifier matches.
        for row in server_registry.list_servers(include_tokens=False):
            if (row.get("machine_identifier") or "") == machine_identifier:
                pin = media_db.get_managed_user_credential(
                    row["id"], username, "plex_home_pin",
                )
                db_access_log.log_read(
                    table="managed_users",
                    field="plex_home_pin_enc",
                    where={"server_id": row["id"], "username": username},
                    intent=(
                        "PIN lookup for home-user authentication" +
                        (" (PIN present)" if pin else " (no PIN stored)")
                    ),
                )
                return pin or None
    except Exception:
        return None
    return None


def _tombstoned_usernames_for_server(machine_identifier: str) -> set:
    """
    PR-13 follow-up: return the set of usernames that should be
    skipped on the server with the given ``machine_identifier``.
    Combines the global tombstones table with the per-server
    tombstone flags on ``managed_users``. Empty set on any error
    (best-effort - we'd rather connect to a hidden user than fail
    the whole run on a registry hiccup).
    """
    if not machine_identifier:
        return set()
    try:
        from server import server_registry, media_db
        from services import db_access_log
    except Exception:
        return set()
    try:
        # Global tombstones first - these apply regardless of server.
        global_set = media_db.list_global_tombstone_usernames()
        db_access_log.log_read(
            table="global_tombstones",
            where={"count": len(global_set)},
            intent="tombstone filter for home-user enumeration",
        )

        server_id = ""
        for row in server_registry.list_servers(include_tokens=False):
            if (row.get("machine_identifier") or "") == machine_identifier:
                server_id = row["id"]
                break
        if not server_id:
            return set(global_set)

        # Per-server tombstones: rows on this server with tombstoned=1.
        per_server: set = set()
        try:
            rows = media_db.list_managed_users(server_id, include_hidden=True)
            for row in rows:
                if row.get("hidden_scope") in ("server", "global"):
                    per_server.add(row["username"])
        except Exception:
            pass
        db_access_log.log_read(
            table="managed_users",
            field="tombstoned",
            where={"server_id": server_id, "hidden_count": len(per_server)},
            intent="per-server tombstone filter for home-user enumeration",
        )
        return set(global_set) | per_server
    except Exception:
        return set()


def get_home_users(
    server: PlexServer,
    base_url: str,
    logger: logging.Logger,
) -> List[Tuple[str, str, PlexServer]]:
    """
    Returns a list of (username, token, server) for each Plex Home managed user.

    Plex Home lets multiple profiles share one server. Each profile has its own
    independent Play Count and star ratings - the admin account's data does not
    include managed users' data. This function authenticates as each managed user
    so their data can be exported and imported separately.

    Args:
        server (PlexServer): Admin server connection (must be linked to Plex.tv).
        base_url (str): Plex server base URL, e.g. "http://localhost:32400".
        logger (Logger): Shared logger.

    Returns:
        List of (username, user_token, user_server) tuples - one per managed user.
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

        # PR-13 follow-up - apply tombstone filters BEFORE we
        # authenticate any user. Pre-fix, the engine connected to
        # every managed user including ones the operator had hidden
        # via the User Management panel; the resulting per-user
        # payload then carried owner-attributed data (fix #4) or
        # data the operator had explicitly asked us not to capture.
        #
        # Two scopes filter here:
        #   * Global tombstones (``global_tombstones`` table) -
        #     username hidden on every server.
        #   * Per-server tombstones (``managed_users.tombstoned``) -
        #     username hidden on this specific server only.
        tombstoned_usernames = _tombstoned_usernames_for_server(
            getattr(server, "machineIdentifier", "") or "",
        )
        if tombstoned_usernames:
            users_before = len(users)
            users = [
                u for u in users
                if (getattr(u, "title", "") or "") not in tombstoned_usernames
            ]
            skipped = users_before - len(users)
            if skipped:
                logger.info(
                    "Tombstone filter: skipped %d hidden managed user(s) "
                    "(global + per-server). Hidden: %s",
                    skipped, ", ".join(sorted(tombstoned_usernames)),
                )

        if not users:
            logger.info(
                "No visible managed users after tombstone filter (every user is hidden)."
            )
            return result

        # Surface the slow per-user auth burst on the dashboard's
        # activity feed - without this the user sees nothing for the
        # 5-30 s it can take to walk every home user, especially when
        # some 401 and trigger plexapi's retry backoff.
        if state.get_dashboard():
            state.get_dashboard().push_activity(
                "phase", "-", f"Authenticating {len(users)} home user(s)…",
            )

        # ── Fetch per-user tokens in parallel ─────────────────────────────────
        # PR-2 / Phase C (auth refactor): PIN-protected managed users.
        # ``user.get_token()`` raises an auth error when the user has a
        # PIN set on the server; pre-PR-2 we logged a warning and
        # silently dropped that user from the roster.
        #
        # PR-2 introduced an "admin-token fallback" that re-used the
        # admin's PlexServer for the failed user. That was a serious
        # data-fidelity bug: ``section.watched()`` filters by the
        # currently-authenticated session, so the admin server returns
        # the OWNER's watched history for every PIN-protected user,
        # producing identical play counts under each managed user's
        # name in the snapshot. See PR-13 fix #4.
        #
        # PR-13 fix #4 reverts to the pre-PR-2 behaviour (drop the
        # user if we can't authenticate them) BUT first tries to use
        # the operator-supplied PIN from ``managed_users.plex_home_pin_enc``
        # (PR-10 storage). If a PIN is stored, we sign in as the home
        # user via the account-level switch and obtain a real per-user
        # token. If no PIN is stored OR sign-in still fails, the user
        # is dropped from the roster with a warning - the pre-flight
        # check (PR-12) surfaces this to the operator before the job
        # commits so they can save the PIN under User Management.
        def _try_account_switch(user):
            """Use signInHomeUser when a PIN is stored. Returns a
            (token, server) tuple or raises if the switch isn't
            possible / fails."""
            stored_pin = _lookup_stored_pin(user.title, server.machineIdentifier)
            if not stored_pin:
                raise RuntimeError("no stored PIN")
            # plexapi's API for home-user sign-in varies between
            # versions; we try the most common shape and fall through
            # the AttributeError on older builds.
            switch_method = (
                getattr(account, "signInHomeUser", None)
                or getattr(account, "switchHomeUser", None)
            )
            if switch_method is None:
                raise RuntimeError(
                    "plexapi build does not expose a home-user sign-in helper"
                )
            try:
                impersonated = switch_method(user, pin=stored_pin)
            except TypeError:
                # Positional pin signature on older plexapi builds.
                impersonated = switch_method(user, stored_pin)
            user_token = getattr(impersonated, "authToken", None) or getattr(impersonated, "_token", None)
            if not user_token:
                raise RuntimeError("PIN-authenticated account exposed no token")
            user_server = PlexServer(base_url, user_token, timeout=120)
            return user_token, user_server

        def _connect_user(user):
            # Wrap the entire per-user auth in _thread_category so the
            # dashboard's Thread Pool panel shows active workers during
            # the home-user fan-out. Pre-fix the panel reported 0 active
            # workers during this phase even though N threads were
            # hitting Plex.tv in parallel.
            from services.dashboard import _thread_category
            with _thread_category("home_user"):
                # 1) Token without PIN (works for unprotected users).
                try:
                    user_token = user.get_token(server.machineIdentifier)
                    user_server = PlexServer(base_url, user_token, timeout=120)
                    return user.title, user_token, user_server, "direct"
                except Exception as direct_err:
                    # 2) Stored PIN -> account-level sign-in.
                    try:
                        user_token, user_server = _try_account_switch(user)
                        return user.title, user_token, user_server, "pin"
                    except Exception as pin_err:
                        # Re-raise the original direct error so the caller
                        # can decide how to log it; chain the PIN error
                        # so it shows up in the warning context.
                        raise RuntimeError(
                            f"direct token failed ({direct_err}); "
                            f"PIN sign-in failed ({pin_err})"
                        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(users)) as pool:
            futs = {pool.submit(_connect_user, u): u for u in users}
            for fut in concurrent.futures.as_completed(futs):
                u = futs[fut]
                try:
                    title, user_token, user_server, mode = fut.result()
                    result.append((title, user_token, user_server))
                    if mode == "pin":
                        logger.info(f"Connected as home user (stored PIN): {title}")
                        # Audit trail: explicit record that the stored
                        # PIN was successfully used to authenticate.
                        # Lets the operator confirm the PIN-fetch path
                        # is firing without grepping the run log.
                        try:
                            from services import db_access_log
                            db_access_log.log_event(
                                "Stored PIN authenticated home-user sign-in for %r on machine %r",
                                title,
                                getattr(server, "machineIdentifier", "") or "?",
                            )
                        except Exception:
                            pass
                    else:
                        logger.info(f"Connected as home user: {title}")
                except Exception as e:
                    # PR-13 fix #4: drop the user, do NOT fall back to
                    # the admin server. Falling back would silently
                    # attribute the owner's watch / rating / playlist
                    # data to this user's row, corrupting per-user
                    # state in every downstream snapshot. The operator
                    # can save the user's PIN under Servers -> User
                    # Management and re-run; PR-12's pre-flight panel
                    # will surface PIN-protected users that lack stored
                    # PINs before the job commits.
                    logger.warning(
                        "Home user %r could not authenticate (%s) - "
                        "user is being DROPPED from this run to avoid "
                        "the owner-watch-bleed bug from PR-2. Save the "
                        "user's Plex Home PIN under Servers -> User "
                        "Management to capture their data on the next run.",
                        u.title, e,
                    )

        if state.get_dashboard():
            state.get_dashboard().push_activity(
                "started", "-",
                f"Home users ready: {len(result)} of {len(users)} authenticated",
            )

    except Exception as e:
        logger.info(
            f"Multi-user support unavailable (requires a Plex.tv-linked account): {e}. "
            f"Only admin account data will be processed."
        )

    return result
