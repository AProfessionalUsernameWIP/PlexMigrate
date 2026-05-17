"""
Plex server authentication and library discovery for PlexMigrate.

Contains token auto-discovery, server connection, library enumeration,
home user token fetching, and the shared HTTP session / retry adapter factory.
"""

import concurrent.futures
import logging
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from plexapi.server import PlexServer
from requests.adapters import HTTPAdapter
from rich.table import Table
from urllib3.util.retry import Retry

import services.state as state
from services.state import PLEX_DB_PATHS, console


log = logging.getLogger("plexmigrate.services.auth")


# ── HTTP Session / Retry Adapter ──────────────────────────────────────────────

def _make_retry_adapter(pool_maxsize: Optional[int] = None) -> HTTPAdapter:
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

    Tunables: ``total`` and ``backoff_factor`` come from
    ``services.tunables`` so an end user can bump retry tolerance for
    flakier upstream Plex servers without a code change. ``pool_maxsize``
    falls back to the tunable when the caller doesn't override it.

    Args:
        pool_maxsize (int, optional): Max simultaneous open connections in
            the pool. When ``None``, the ``http_pool_maxsize_cap`` tunable
            is used.
    """
    # Lazy import so this module stays importable from CLI-only checkouts
    # that don't have services.tunables on the path (e.g. early test
    # bootstrap). Falling back to the literals preserves prior behaviour.
    try:
        from services import tunables
        total = int(tunables.plex_retry_total_budget())
        backoff = float(tunables.plex_retry_backoff_factor())
        pool_connections = int(tunables.http_pool_connections())
        if pool_maxsize is None:
            pool_maxsize = int(tunables.http_pool_maxsize_cap())
    except Exception:
        total = 4
        backoff = 0.5
        pool_connections = 4
        if pool_maxsize is None:
            pool_maxsize = 10

    retry = Retry(
        total=total,
        connect=2,
        read=1,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "PUT"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    return HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )


# Registry of every requests.Session the engine has handed out, kept
# as a weak set so a Session that goes out of scope doesn't keep the
# entry alive. ``invalidate_sessions()`` walks this list and re-mounts
# each session's HTTPAdapter from the current tunables.
import weakref

_LIVE_SESSIONS: "weakref.WeakSet[requests.Session]" = weakref.WeakSet()
_LIVE_SESSIONS_LOCK = threading.Lock()


def _register_session(session: requests.Session) -> None:
    """Track ``session`` so it can be rebuilt on a tunable change."""
    with _LIVE_SESSIONS_LOCK:
        _LIVE_SESSIONS.add(session)


def invalidate_sessions() -> None:
    """
    Rebuild every live Plex requests.Session with the current tunables.

    Called by ``server.persistence.save_settings`` when one of the
    HTTP-related tunables (retry budget, backoff, pool sizes,
    timeouts) actually changed. Walks the weak registry of sessions
    and re-mounts a fresh ``_make_retry_adapter`` on each. Existing
    in-flight requests aren't cancelled - they finish on the old
    adapter; subsequent requests use the new one.

    Phase 1 stub: the weak set is empty until Phase 3 wires
    ``_make_session`` / ``connect_to_server`` to register sessions.
    Calling this now is a safe no-op.
    """
    with _LIVE_SESSIONS_LOCK:
        snapshot = list(_LIVE_SESSIONS)
    if not snapshot:
        return
    for session in snapshot:
        try:
            adapter = _make_retry_adapter()
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        except Exception:  # pragma: no cover (defensive)
            log.exception("invalidate_sessions: failed to rebuild adapter on a session")


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
    _register_session(session)
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


def connect_to_server(
    url: str,
    token: str,
    logger: logging.Logger,
    *,
    raise_on_failure: bool = False,
) -> Optional[PlexServer]:
    """
    Connects to a Plex Media Server and returns the connection object.

    Also mounts a retry adapter onto plexapi's internal session so GUID
    lookups, section.search(), and section.all() benefit from the same
    retry policy as our own HTTP calls.

    Args:
        url (str): Full server URL including protocol and port.
        token (str): Plex authentication token.
        logger (Logger): Shared logger for recording the outcome.
        raise_on_failure (bool): When True, re-raise the underlying
            exception instead of returning None on connect failure.
            End user-facing probe / test paths set this so the actual
            reason (TLS error, 401, plexapi-specific exception, etc.)
            reaches the UI rather than being collapsed into a generic
            "Plex unreachable" message. Engine paths leave the default
            False so a transient connect failure surfaces as a
            graceful None for the caller's own retry / fallback logic.

    Returns:
        PlexServer if connection succeeds. None if it fails AND
        ``raise_on_failure`` is False. Raises the underlying exception
        when ``raise_on_failure`` is True.
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
            # Phase 1 hot-reload: track every plexapi session so a
            # later HTTP-tunable change can rebuild the adapter via
            # invalidate_sessions().
            _register_session(server._session)

        logger.info(
            f"Connected to Plex server: {server.friendlyName} (version {server.version})"
        )
        return server

    except Exception as e:
        # logger.exception (not .error) so the FULL traceback lands
        # in the backend log. The exception class name plus the
        # plexapi-specific message is what an end user needs to
        # distinguish "token rejected" from "TLS handshake failed"
        # from "URL malformed" - useless if the stack frames are
        # stripped. The single error line stays as the leading
        # message so existing log greppers still match.
        logger.exception(f"Failed to connect to Plex server at {url}: {e}")
        if raise_on_failure:
            raise
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
    ``db_access.log`` so the end user can confirm the stored-PIN
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


def _share_state_for_server(machine_identifier: str) -> Dict[str, Dict[str, Any]]:
    """
    Return ``{username: {"active_share": bool, "is_pin_protected": bool,
    "has_token": bool, "has_pin": bool, "refreshed_at": float|None}}``
    for every cached managed-user row on the server identified by
    ``machine_identifier``. Empty dict on any error.

    The engine consults this map before fanning out per-user
    authentication so we can:

      * Skip rows the end user has effectively un-shared on Plex.tv
        (``active_share=False``) with a single explanatory log line
        instead of N noisy "could not authenticate" warnings.
      * Emit a targeted "PIN-protected, save PIN under User Management"
        log when a direct token fetch fails on a row Plex.tv flagged
        as ``protected=1``.

    Best-effort: a registry / DB hiccup returns an empty dict, which
    makes the gate a no-op and preserves prior behaviour (every
    ``account.users()`` row goes through the auth fan-out).
    """
    if not machine_identifier:
        return {}
    try:
        from server import server_registry, media_db
    except Exception:
        return {}
    try:
        server_id = ""
        for row in server_registry.list_servers(include_tokens=False):
            if (row.get("machine_identifier") or "") == machine_identifier:
                server_id = row["id"]
                break
        if not server_id:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for row in media_db.list_managed_users(server_id, include_hidden=True):
            uname = row.get("username") or ""
            if not uname:
                continue
            out[uname] = {
                "active_share": bool(row.get("active_share", True)),
                "is_pin_protected": bool(row.get("is_pin_protected", False)),
                "has_token": bool(row.get("has_token")),
                "has_pin": bool(row.get("has_pin")),
                "refreshed_at": row.get("shared_state_refreshed_at"),
                "kind": row.get("kind") or "managed",
            }
        return out
    except Exception:
        return {}


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
    *,
    user_filter: Optional[Iterable[str]] = None,
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
        user_filter (Iterable[str], optional): 2026-05-17 (operator
            request): when provided, pre-filter the home-user list to
            ONLY usernames present in the iterable BEFORE authenticating.
            Saves a 5-30s burst per excluded user (each gets its own
            ``user.get_token()`` round-trip with potential PIN auth).
            Owner-only / single-user runs that used to wait for every
            home user to auth now skip every user that wasn't picked.
            ``None`` (default) preserves the legacy "auth everyone" path.
            Case-insensitive match against ``user.username``.

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

        # 2026-05-17 (operator request): pre-auth user_filter. The
        # snapshotter / restorer / direct-transfer callers select a
        # subset of users on the UI side; this is the first chance to
        # narrow the auth burst to JUST those usernames. Each excluded
        # user saves a slow ``user.get_token()`` round-trip (+ optional
        # PIN-auth retries). Case-insensitive username match.
        if user_filter is not None:
            wanted = {str(s).strip().lower() for s in user_filter if str(s).strip()}
            if wanted:
                users_before = len(users)
                users = [
                    u for u in users
                    if (getattr(u, "title", "") or "").strip().lower() in wanted
                ]
                skipped = users_before - len(users)
                if skipped:
                    logger.info(
                        "user_filter applied: authenticating %d of %d "
                        "home user(s) (skipped %d not in selection: %s)",
                        len(users), users_before, skipped,
                        ", ".join(sorted(wanted)),
                    )
            else:
                # Empty / whitespace-only filter — explicit "no managed
                # users" signal. Skip the entire auth path.
                logger.info(
                    "user_filter is an empty set; skipping all home-user auth.",
                )
                return result

        if not users:
            logger.info(
                "No managed users remain after user_filter; skipping auth burst.",
            )
            return result

        # PR-13 follow-up - apply tombstone filters BEFORE we
        # authenticate any user. Pre-fix, the engine connected to
        # every managed user including ones the end user had hidden
        # via the User Management panel; the resulting per-user
        # payload then carried owner-attributed data (fix #4) or
        # data the end user had explicitly asked us not to capture.
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

        # ── Share-state gate (2026-05-15; cross-server-leak fix 2026-05-17) ─
        # ``account.users()`` returns every friend / managed user the
        # admin has *ever* shared with - GLOBALLY across every server
        # the owner has linked, including users who have NO share on
        # the server we're currently snapshotting. The local
        # ``managed_users`` cache (populated by
        # ``sync_managed_users_from_live`` on every server-connect probe)
        # is the authoritative per-server roster: every row in it
        # represents a user with access to THIS server, and every user
        # with access to this server IS in the cache after a successful
        # sync.
        #
        # 2026-05-17 cross-server-leak fix: when the cache is populated
        # for this server (non-empty), a user NOT in the cache is a
        # user from another server, NOT a "new user the sync hasn't
        # picked up yet". Drop them silently from the auth fan-out so
        # the engine doesn't waste 30s per server-not-mine user trying
        # tokens that 401 + emit the misleading "save the user's PIN
        # under User Management" message (the user has no row to save
        # a PIN on; they don't exist on this server).
        #
        # Pre-fix the "info is None -> assume active" branch was
        # KEEPING those cross-server users, then trying to auth them
        # with the admin token (which is scoped to this server), then
        # emitting N warnings of the form:
        #   "Home user 'Adam abu-issa' could not authenticate ... save
        #    the user's Plex Home PIN under User Management"
        # despite Adam having no row in this server's managed_users
        # table at all. The cache stays empty only on the absolute
        # first connect to this server (before the share-state sync
        # has ever run); in that one case we fall through to the
        # legacy "keep everything" semantics so we don't silently
        # hide users on a brand-new install.
        share_state = _share_state_for_server(
            getattr(server, "machineIdentifier", "") or "",
        )
        if share_state:
            stale = []           # cached but no active share (sync says off)
            cross_server = []    # not in this server's cache at all
            kept = []
            for u in users:
                uname = (getattr(u, "title", "") or "")
                info = share_state.get(uname)
                if info is None:
                    # User isn't in this server's managed_users cache
                    # despite the cache having rows -> they don't have
                    # a share here. Drop silently.
                    cross_server.append(uname)
                    continue
                if info.get("active_share", True):
                    kept.append(u)
                else:
                    stale.append(uname)
            if cross_server:
                logger.info(
                    "Share-state filter: skipped %d Plex Home user(s) "
                    "with no share on this server (they appear on the "
                    "owner's Plex.tv roster but have no access here). "
                    "Skipped: %s. This is normal when the owner runs "
                    "multiple Plex servers - account.users() returns "
                    "the global Home roster, not the per-server share "
                    "list.",
                    len(cross_server),
                    ", ".join(sorted(cross_server)),
                )
            if stale:
                logger.info(
                    "Share-state filter: skipped %d managed user(s) with "
                    "no active share on this server (Plex.tv reports the "
                    "share has been removed). Skipped: %s. Use "
                    "Servers -> Users -> Refresh shared state to update "
                    "the cache.",
                    len(stale), ", ".join(sorted(stale)),
                )
            users = kept

        if not users:
            logger.info(
                "No managed users with an active share on this server."
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
        # the end user-supplied PIN from ``managed_users.plex_home_pin_enc``
        # (PR-10 storage). If a PIN is stored, we sign in as the home
        # user via the account-level switch and obtain a real per-user
        # token. If no PIN is stored OR sign-in still fails, the user
        # is dropped from the roster with a warning - the pre-flight
        # check (PR-12) surfaces this to the end user before the job
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
                        # Lets the end user confirm the PIN-fetch path
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
                    # state in every downstream snapshot. The end user
                    # can save the user's PIN under Servers -> User
                    # Management and re-run; PR-12's pre-flight panel
                    # will surface PIN-protected users that lack stored
                    # PINs before the job commits.
                    info = share_state.get(getattr(u, "title", "") or "", {})
                    if info.get("is_pin_protected") and not info.get("has_pin"):
                        # Targeted message: Plex.tv confirms this user
                        # has a PIN set, but we don't have it stored.
                        logger.warning(
                            "Home user %r is PIN-protected on Plex.tv but "
                            "no PIN is stored locally; dropping from this "
                            "run. Save the PIN under Servers -> User "
                            "Management to capture their data next run.",
                            u.title,
                        )
                    elif info.get("is_pin_protected"):
                        # PIN stored but switch still failed - likely a
                        # stale / wrong PIN. Tell the end user exactly
                        # what to check.
                        logger.warning(
                            "Home user %r is PIN-protected and a stored "
                            "PIN was tried but Plex rejected it (%s); "
                            "dropping from this run. Update the stored "
                            "PIN under Servers -> User Management.",
                            u.title, e,
                        )
                    else:
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
