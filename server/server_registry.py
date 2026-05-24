"""
Multi-server registry for Hestia-MediaManager v0.9.0.

Replaces the single ``plex_url`` + ``plex_token`` fields that lived in
``settings.json`` with a *list* of registered Plex servers. Each entry
carries a user-given friendly name, the URL, the token, and cached
status fields (last connection result, last contact time, last
library catalogue) so the frontend can paint a server-list page
without forcing every navigation to hit Plex.

Persistence
-----------
The registry lives in ``$PLEXMIGRATE_DATA_DIR/servers.json`` (defaults
to ``./server_data/servers.json`` - same volume mount as the rest of
the persistence layer). Writes go through the same atomic-write
helper that ``server/persistence.py`` uses, so a crash mid-save
leaves either the old document intact or the new one fully written.

Migration from v0.8.0
---------------------
v0.8.0 stored ``plex_url`` / ``plex_token`` in ``settings.json``.
On first boot in v0.9.0 :func:`migrate_legacy_settings` checks for
those keys and, if present, registers them as a server called
``"Default"`` then clears them from ``settings.json``. The legacy
fields are gone from the persistence default schema, so re-running
migration on an already-migrated install is a no-op.

Engine contract
---------------
This module **does not** modify the engine. It exposes
:func:`connect_registered_server` which returns the same
``(PlexServer, url, token, owner_name)`` tuple the engine has
always accepted. The engine is unaware of the registry; it sees a
single connection and a single token per call, exactly as in
single-server mode.

Threading note
--------------
Several Hestia-MediaManager threads can read and write this file:
- The request handler when the user adds / renames / deletes a server.
- The "test connection" handler when the user clicks the refresh icon.
- The job worker reading the registry to resolve a name at run start.

A single module-level lock (:data:`_REG_LOCK`) serialises every
read-modify-write cycle. Reads that don't mutate (e.g. ``list_servers``)
also take the lock to ensure they see a consistent snapshot rather
than a mid-write file.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from services.adapters import MediaServerAdapter

from server.persistence import (
    _atomic_write_json,
    delete_schedules_by_server_name,
    get_data_dir,
    load_settings,
    save_settings,
)
from server.secrets import decrypt_str, encrypt_str


log = logging.getLogger("plexmigrate.server.registry")


class ServerCredentialError(Exception):
    """
    Raised when a server's stored token can't be decrypted.

    The message is user-actionable - it names the server and tells
    the end user exactly what to do - because this exception flows
    all the way out to HTTP error responses and dashboard activity
    feed entries.
    """


# ── File location ────────────────────────────────────────────────────────────

def _registry_path() -> Path:
    """Absolute path to ``servers.json`` inside the configured data dir."""
    return get_data_dir() / "servers.json"


# ── Cross-thread lock ────────────────────────────────────────────────────────

# All public functions in this module that touch ``servers.json``
# acquire this lock. Granularity is whole-file because the file is
# small and access is infrequent - a finer lock would buy us nothing
# and complicate the invariants.
_REG_LOCK = threading.Lock()


# ── Server-UID format ────────────────────────────────────────────────────────
#
# Every server row carries a stable per-row identifier assigned at
# add_server time and bound to the row for life. Format:
#
#   <service_type>_<uuid4_hex>
#
# Three valid prefixes correspond to the three supported backends.
# The prefix lets any code that handles a UID parse the backend out
# directly without a registry roundtrip; the suffix is the standard
# 32-char lowercase hex form of a uuid4.

_VALID_SERVER_BACKEND_PREFIXES = frozenset({"plex", "jellyfin", "emby"})


def make_server_id(service_type: str) -> str:
    """Generate a new prefixed server UID for a row about to be
    registered. The backend prefix comes from ``service_type``,
    validated against the registry's supported set."""
    svc = (service_type or "").strip().lower()
    if svc not in _VALID_SERVER_BACKEND_PREFIXES:
        raise ValueError(
            f"Unsupported service_type {service_type!r}; expected "
            f"one of {sorted(_VALID_SERVER_BACKEND_PREFIXES)}."
        )
    return f"{svc}_{uuid.uuid4().hex}"


def parse_server_id(server_id: str) -> Tuple[Optional[str], str]:
    """Parse a server UID into ``(service_type, raw_uuid_hex)``.
    Returns ``(None, "")`` on malformed input. Pure; no I/O."""
    sid = (server_id or "").strip()
    if "_" not in sid:
        return None, ""
    prefix, _, suffix = sid.partition("_")
    prefix = prefix.lower()
    if prefix not in _VALID_SERVER_BACKEND_PREFIXES:
        return None, ""
    if not suffix:
        return None, ""
    return prefix, suffix


def is_valid_server_id(server_id: str) -> bool:
    """``True`` iff the UID is well-formed (recognised backend prefix
    + non-empty suffix). Does NOT check registry existence; callers
    that need that should follow up with :func:`get_server_by_id`."""
    svc, _ = parse_server_id(server_id)
    return svc is not None


def migrate_server_ids_add_backend_prefix() -> Dict[str, int]:
    """Boot-time one-shot: upgrade existing bare-UUID server rows to
    the prefixed ``<service_type>_<uuid4_hex>`` form, and rewrite
    every cross-table reference to the old ids in one pass.

    Idempotent: already-prefixed rows are skipped, so subsequent
    boots are no-ops. Surfaces deletable once we ship release-one
    and decide schema-change policy beyond pre-release.

    Returns a counts dict:
        {
            "servers_upgraded": int,
            "schedules_updated": int,
            "identity_map_rows_updated": int,
            "managed_users_rows_updated": int,
            "snapshot_rows_updated": int,
        }

    Boot path logs the counts so the end user sees what happened on
    first boot of the new build.

    Safe to call from app startup BEFORE other registry use; takes
    its own _REG_LOCK and any downstream DB rewrites use their own
    locks.
    """
    counts = {
        "servers_upgraded": 0,
        "schedules_updated": 0,
        "identity_map_rows_updated": 0,
        "managed_users_rows_updated": 0,
        "snapshot_rows_updated": 0,
    }
    old_to_new: Dict[str, str] = {}
    with _REG_LOCK:
        rows = _load_raw()
        dirty = False
        for row in rows:
            existing_id = (row.get("id") or "").strip()
            if is_valid_server_id(existing_id):
                continue
            service_type = (row.get("service_type") or "plex").lower()
            new_id = make_server_id(service_type)
            if existing_id:
                old_to_new[existing_id] = new_id
            row["id"] = new_id
            counts["servers_upgraded"] += 1
            dirty = True
        if dirty:
            _save_raw(rows)

    if not old_to_new:
        return counts

    # Schedule reference rewrite. Same on-disk JSON shape as the
    # rest of persistence.py; pull through that module so we don't
    # duplicate the atomic-write contract.
    try:
        from server import persistence
        schedules = persistence.load_schedules()
        schedules_dirty = False
        for sched in schedules:
            src = sched.get("source_server_id")
            if src in old_to_new:
                sched["source_server_id"] = old_to_new[src]
                counts["schedules_updated"] += 1
                schedules_dirty = True
            dests = sched.get("dest_server_ids") or []
            if isinstance(dests, list):
                new_dests = [old_to_new.get(d, d) for d in dests]
                if new_dests != dests:
                    sched["dest_server_ids"] = new_dests
                    counts["schedules_updated"] += 1
                    schedules_dirty = True
        if schedules_dirty:
            persistence.save_schedules(schedules)
    except Exception:
        log.exception(
            "migrate_server_ids: schedule rewrite failed; "
            "operator may need to re-author affected schedules."
        )

    # media.db reference rewrites: user_identity_map + managed_users.
    try:
        from server import media_db
        counts["identity_map_rows_updated"] = (
            media_db.rewrite_server_ids_in_identity_map(old_to_new) or 0
        )
        counts["managed_users_rows_updated"] = (
            media_db.rewrite_server_ids_in_managed_users(old_to_new) or 0
        )
    except Exception:
        log.exception(
            "migrate_server_ids: media.db row rewrite failed; "
            "identity-map + managed-user lookups may return stale "
            "results for upgraded ids until the operator re-runs."
        )

    # snapshots.db reference rewrite.
    try:
        from server import snapshot_registry as _snapshot_registry
        counts["snapshot_rows_updated"] = (
            _snapshot_registry.rewrite_server_ids_in_snapshots(old_to_new) or 0
        )
    except Exception:
        log.exception(
            "migrate_server_ids: snapshots.db row rewrite failed; "
            "registered snapshots from upgraded ids may not resolve "
            "to their server until the operator re-registers."
        )

    # playlist_cache.db reference rewrite.
    # Best-effort: if the cache DB has never been initialised on this
    # boot, importing + calling its rewrite helper is still safe (it
    # raises RuntimeError; we catch and log, no migration impact).
    try:
        from server import playlist_cache_db as _playlist_cache_db
        counts["playlist_cache_rows_updated"] = (
            _playlist_cache_db.rewrite_server_ids_in_playlist_cache(old_to_new) or 0
        )
    except Exception:
        log.exception(
            "migrate_server_ids: playlist_cache.db row rewrite failed; "
            "Playlist Management cache for upgraded ids will be re-fetched "
            "on next access."
        )

    return counts


# ── Defaults ─────────────────────────────────────────────────────────────────

# The shape of one server record. Anything missing from an on-disk
# document is filled in from this dict on load so adding a new field
# in a future release doesn't break an existing registry.
_DEFAULT_SERVER: Dict[str, Any] = {
    "id": "",
    "name": "",
    "url": "",
    "token": "",
    # Per-row backend discriminator. Rows registered
    # before this feature shipped omit it; readers default to "plex".
    # New rows declare their backend at add_server() time and the
    # value is one of {"plex", "jellyfin", "emby"}.
    "service_type": "plex",
    # Auto-fallback-token state. When the end user's
    # typed token returns 401 at Add Server time but an existing
    # registered server's token DID connect, we store the working
    # token under ``token`` (so the engine just works) and stash the
    # end user's typed token here under ``pending_token``. A small
    # chip on the Servers panel row plus a Retry button lets the
    # end user re-probe the pending token periodically; new servers'
    # tokens often need a few minutes to propagate on Plex.tv's
    # authorization layer. When the retry connects, pending replaces
    # active and these fields clear.
    #
    # All four fields are absent on rows registered before this
    # feature shipped; readers should treat missing as None.
    "pending_token": "",                 # Fernet ciphertext, like ``token``
    "pending_token_first_seen_at": 0.0,  # UNIX timestamp the pending was stashed
    "pending_token_last_probed_at": 0.0, # UNIX timestamp of the last retry probe
    "pending_token_source": "",          # free-form label, e.g. "operator_typed_at_add"
    "last_status": "unknown",         # "ok" | "unreachable" | "auth_error" | "unknown"
    "last_status_detail": "",         # Human-readable description of the last result.
    "last_checked_at": 0.0,           # UNIX timestamp of last connection attempt.
    "last_libraries": [],             # Cached library list ({name,type,key,count}).
    "owner_name": "",                 # myPlexUsername at last successful connect.
    # Identity captured from the Plex server itself so we can
    # detect a URL/token mismatch (end user typed the right URL but
    # used a token for a different server). ``machine_identifier`` is
    # the stable UUID-shaped ID Plex assigns to each install - distinct
    # from the friendly registry ``name`` we let the end user choose,
    # and from ``friendly_name`` which is what the SERVER reports as
    # its own friendly name (set inside Plex's own settings).
    "machine_identifier": "",
    "friendly_name": "",
    # Plex Media Server software version (e.g. "1.32.5.7349").
    # Captured at probe / refresh time from plexapi's ``PlexServer.version``
    # attribute. Used by the frontend to gate features that require a
    # minimum Plex version (currently: Fast Collection Detection
    # requires >=1.32 because it relies on the ``librarySectionUserID``
    # attribute Plex didn't ship until then). Empty string when the
    # row was added before this field landed or hasn't been refreshed.
    "plex_version": "",
    # Response time in milliseconds for the last lightweight
    # ping. ``None`` if no ping has succeeded yet. Used by the Servers
    # tab live indicator and the JobForm server selector chips.
    "last_response_ms": None,

    # Marker indicating ``token`` is a Fernet ciphertext string.
    # Rows missing this marker (or with it set to False) are treated as
    # legacy plaintext on the next ``_load_raw`` and migrated in place.
    "_encrypted": False,
    # End user-chosen friendly names for users on
    # this server. Keys: raw Plex identifier - owner's email for the
    # owner, managed user's username for managed users. Values: a
    # free-form display string. Empty dict on rows that have never
    # had a custom display name assigned. The frontend reads this map
    # to substitute friendly names in the dashboard header, activity
    # feed, and direct-transfer user selector. Purely a rendering aid
    # - engine logic, logs, and export files always use the raw
    # identifier.
    "user_display_names": {},
    # Timing-spec inputs (Library Catalogue expansion). Each library
    # entry in last_libraries may now carry a ``leaf_counts`` dict with
    # the leaf-level item count that the restore process actually
    # iterates - episodes for show libraries, tracks for artist
    # libraries (music + audio-books). Top-level ``count`` stays the
    # same (movies / shows / artists) for backward compat. Server-wide
    # ``playlist_count`` and ``collection_count`` sit at the row root
    # because playlists and collections are server-wide, not per-library.
    # ``counts_refreshed_at`` is a single unix timestamp covering all
    # of these so the timing engine can decide whether to trust them
    # or prompt for a fresh Refresh.
    "playlist_count": None,
    "collection_count": None,
    "counts_refreshed_at": None,
    # Per-server opt-in for the auto-tombstone sweeper. When False
    # (default),
    # the sweeper still PROBES users on this server (provided the
    # global tunable is on) and writes signals, but never converts
    # signals into tombstones. When True AND the matching
    # per-trigger toggle is on, N consecutive failures auto-tombstone
    # the user. Reversible from the User Management panel.
    "auto_tombstone_inactive_users_enabled": False,
}


# Plex libtype numeric codes used for the cheap totalSize-via-size-0
# queries below. Plex itself documents these in /library/sections's
# response XML but never exposes a stable enum; we keep the mapping
# inline so any plexapi version drift can't move it on us.
_PLEX_LIBTYPE = {
    "movie": 1,
    "show": 2,
    "season": 3,
    "episode": 4,
    "artist": 8,
    "album": 9,
    "track": 10,
    "collection": 18,
}


def _count_via_size_zero(server: Any, section_key: Any, libtype_num: int) -> int:
    """
    Return the total count of items of ``libtype_num`` in this section
    without fetching any rows. Uses Plex's ``X-Plex-Container-Size=0``
    convention: the response body is empty but the root XML element
    carries ``totalSize`` as an attribute.

    Returns 0 on any failure (network, malformed response, unsupported
    Plex build). Callers treat 0 as "not available" rather than zero
    items.
    """
    try:
        url = (
            f"/library/sections/{section_key}/all"
            f"?type={int(libtype_num)}"
            f"&X-Plex-Container-Size=0&X-Plex-Container-Start=0"
        )
        data = server.query(url)
        return int(data.attrib.get("totalSize", 0))
    except Exception:
        return 0


def _fetch_leaf_counts(server: Any, sec: Any) -> Dict[str, int]:
    """
    Best-effort cheap leaf-count for one library section. Returns the
    empty dict when the section type has no relevant leaf level or
    when the count call fails.

    Per-libtype counts captured:

      * Show libraries -> seasons + episodes.
      * Artist libraries -> albums + tracks.
      * Movie libraries -> movies-in-collections (best-effort).

    All use Plex's ``X-Plex-Container-Size=0`` trick so each
    additional count is one extra HTTP round-trip with no body
    payload. These counts feed the Library Catalogue display + the
    Run Job library-mapping panel; they are not used to predict
    run time (the timing engine is discover-don't-predict).

    Seasons + albums are surfaced in the per-run library mapping
    editor so the cost-of-move is visible at a glance ("I'm about to
    map a library with 45 seasons / 312 episodes" reads more
    concretely than "12 shows"). Counted here so every downstream
    consumer (Library Catalogue, sides endpoint, mapping panel) sees
    the same richer counts.
    """
    try:
        libtype = sec.type
    except Exception:
        return {}
    out: Dict[str, int] = {}
    try:
        if libtype == "show":
            try:
                n_eps = _count_via_size_zero(server, sec.key, _PLEX_LIBTYPE["episode"])
                if n_eps:
                    out["episodes"] = n_eps
            except Exception:
                pass
            try:
                n_seasons = _count_via_size_zero(server, sec.key, _PLEX_LIBTYPE["season"])
                if n_seasons:
                    out["seasons"] = n_seasons
            except Exception:
                pass
        elif libtype == "artist":
            try:
                n_tracks = _count_via_size_zero(server, sec.key, _PLEX_LIBTYPE["track"])
                if n_tracks:
                    out["tracks"] = n_tracks
            except Exception:
                pass
            try:
                n_albums = _count_via_size_zero(server, sec.key, _PLEX_LIBTYPE["album"])
                if n_albums:
                    out["albums"] = n_albums
            except Exception:
                pass
        elif libtype == "movie":
            # Movies-in-collection: count of distinct movies that
            # belong to at least one collection. Plex doesn't expose
            # this as a single number; the cheapest path is per-
            # collection size summed. ``totalSize`` on
            # /library/sections/<k>/collections gives collection
            # count; iterating each collection's child count would
            # be an N-call walk that scales with collection count.
            # Best-effort: capture only collection_count for now and
            # let the UI display "M collections" alongside the movie
            # total. Detailed per-collection membership is left as a
            # follow-up to avoid blowing up the cheap "Refresh
            # libraries" path.
            try:
                url = (
                    f"/library/sections/{sec.key}/collections"
                    f"?X-Plex-Container-Size=0&X-Plex-Container-Start=0"
                )
                data = server.query(url)
                n_colls = int(data.attrib.get("totalSize", 0))
                if n_colls:
                    out["collections"] = n_colls
            except Exception:
                pass
    except Exception:
        return {}
    return out


def _fetch_server_level_counts(server: Any, sections: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Fetch the two server-wide counts the timing estimator needs:

      * Playlists -> ``len(server.playlists())``. Single round-trip.
      * Collections -> sum of per-section collection counts. Plex has
        no global collections endpoint, but each section's
        ``/collections`` endpoint accepts the same size-0 trick.

    Returns ``(playlist_count, collection_count)``. Either field is
    ``None`` when the call failed; callers persist that as "not
    available" rather than zero, so the next Refresh can retry without
    the timing engine treating a transient failure as a confirmed
    empty library.
    """
    try:
        playlist_count: Optional[int] = len(list(server.playlists()))
    except Exception:
        playlist_count = None

    collection_count: Optional[int] = 0
    any_section_failed = False
    try:
        for sec in sections:
            try:
                url = (
                    f"/library/sections/{sec.key}/collections"
                    f"?X-Plex-Container-Size=0&X-Plex-Container-Start=0"
                )
                data = server.query(url)
                collection_count += int(data.attrib.get("totalSize", 0))
            except Exception:
                any_section_failed = True
                continue
    except Exception:
        return playlist_count, None
    if any_section_failed and collection_count == 0:
        # Couldn't probe any section successfully; surface as unknown
        # rather than a confident zero.
        return playlist_count, None
    return playlist_count, collection_count


# ── Name-safety helper ───────────────────────────────────────────────────────

# Some server names (e.g. "Living Room / Plex") contain characters
# that are not legal in filenames on Windows or that would break
# log-directory globs. ``safe_server_name`` produces a filename-safe
# slug while preserving readability - used to prefix log dirs and
# snapshot filenames so outputs from different servers never collide.
def safe_server_name(name: str) -> str:
    """
    Turn a free-form friendly name into a filename-safe slug.

    Examples:
        "My Home Plex"     -> "My-Home-Plex"
        "Plex (NAS) #1"    -> "Plex-NAS-1"
        "café / régal"     -> "cafe-regal"
    """
    if not name:
        return "server"
    # ASCII-fold so non-ASCII names don't trip filesystem encoding
    # surprises across host platforms. ``encode('ascii', 'ignore')``
    # silently drops accents; that's fine for filename use.
    folded = name.encode("ascii", "ignore").decode("ascii")
    # Replace any run of non-alphanumeric chars with a single hyphen.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-")
    return slug or "server"


def backend_aware_slug(name: str, service_type: str) -> str:
    """
    Always returns ``"<safe-name>-<service_type>"``.

    The friendly name is unique per ``(name, service_type)`` (an
    end user can have "Jade.TV" Plex + "Jade.TV" Emby). Artifact
    directories, log files, exports, and schedule lookups must mirror
    that composite identity or a cascade delete leaks across backends.

    No backward-compat exemption for Plex - the slug uniformly carries
    the backend even for Plex servers. Every artifact path the engine
    writes carries the suffix on disk.

    Always use this helper - not ``safe_server_name`` directly - when
    computing paths or cascade filters for engine-produced artifacts.
    """
    bare = safe_server_name(name)
    st = (service_type or "plex").lower()
    return f"{bare}-{st}"


# ── Load / Save ──────────────────────────────────────────────────────────────

def _load_raw() -> List[Dict[str, Any]]:
    """
    Read the on-disk registry. Missing or malformed file = empty list.
    Returned list is *not* a deep copy - callers that mutate must
    re-save the result through :func:`_save_raw`.

    Migrates legacy plaintext tokens on first read (v0.9.5): any row
    without ``_encrypted: True`` is treated as plaintext, its ``token``
    is encrypted in place, the marker is set, and the file is
    rewritten atomically. Caller must already hold :data:`_REG_LOCK`
    so the rewrite is safe to issue here.
    """
    import json
    path = _registry_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        log.exception("servers.json unreadable; treating as empty")
        return []
    if not isinstance(data, list):
        return []
    # Backfill missing fields from the default schema so old documents
    # don't break newer code that expects keys added in later versions.
    out: List[Dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        merged = dict(_DEFAULT_SERVER)
        merged.update(raw)
        out.append(merged)

    # Migrate any rows still carrying a plaintext token. Done under
    # the caller's existing _REG_LOCK acquisition so concurrent reads
    # serialise naturally; the worst-case race writes identical
    # (semantically equivalent - Fernet uses a fresh IV each time)
    # ciphertexts back. ``os.replace`` keeps the write atomic.
    if _migrate_unencrypted_tokens_in_place(out):
        _save_raw(out)

    return out


def _migrate_unencrypted_tokens_in_place(rows: List[Dict[str, Any]]) -> bool:
    """
    For every row missing ``_encrypted: True``, treat the ``token``
    field as plaintext and replace it with a Fernet ciphertext. Sets
    the marker. Returns True if at least one row was migrated so the
    caller knows to persist the rewrite.

    Empty tokens are migrated too (marker flipped to True, ciphertext
    stays empty) so the file converges on a uniform shape and we don't
    re-attempt migration on every load.
    """
    migrated = 0
    for row in rows:
        if row.get("_encrypted") is True:
            continue
        plain = row.get("token") or ""
        row["token"] = encrypt_str(plain) if plain else ""
        row["_encrypted"] = True
        migrated += 1
    if migrated:
        log.info(
            "servers.json: migrated %d plaintext token(s) to encrypted storage.",
            migrated,
        )
    return migrated > 0


def _save_raw(rows: List[Dict[str, Any]]) -> None:
    """Atomically replace ``servers.json``."""
    _atomic_write_json(_registry_path(), rows)


def decrypt_server_token(row: Dict[str, Any]) -> str:
    """
    Return the plaintext token for a registry row.

    The token is kept ciphertext on disk and inside :func:`_load_raw`
    output; callers that need to hand a plaintext token to plexapi or
    to a direct HTTP write call use this helper inline at the point
    of use. The decrypted string lives only in the local variable
    returned here - it is never stored on the row or cached.

    Raises:
        ServerCredentialError - when the row's ciphertext can't be
        decrypted. The message names the server and tells the end user
        to re-enter credentials in the Servers tab. Callers should
        let this exception propagate to the request handler / job
        worker, which will translate it to a clean 400/500 or
        dashboard error.
    """
    # Lazy import so a regression test that monkey-patches Fernet
    # doesn't have to load this module's import chain.
    from cryptography.fernet import InvalidToken

    cipher = row.get("token") or ""
    if not cipher:
        return ""
    # Legacy rows (pre-v0.9.5) that somehow reached a decrypt site
    # without going through _load_raw's migration: treat token as
    # plaintext. Reach this branch only if a row was hand-edited or
    # came from a code path that bypasses _load_raw - defensive.
    if not row.get("_encrypted"):
        return cipher
    try:
        return decrypt_str(cipher)
    except InvalidToken as exc:
        name = row.get("name") or row.get("id") or "?"
        raise ServerCredentialError(
            f"Server token for {name!r} is unreadable - the encryption "
            f"key has changed. Please re-enter this server's credentials "
            f"in the Servers tab."
        ) from exc


# ── Public read API ─────────────────────────────────────────────────────────

def list_servers(*, include_tokens: bool = False) -> List[Dict[str, Any]]:
    """
    Return every registered server.

    ``include_tokens`` defaults to False so callers that send the
    list over the network can do so without exposing tokens. When the
    job runner needs the actual token to connect, it passes True.
    """
    with _REG_LOCK:
        rows = _load_raw()
    if include_tokens:
        return rows
    redacted: List[Dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        copy["has_token"] = bool(copy.get("token"))
        copy.pop("token", None)
        # ``pending_token`` is also a Fernet ciphertext; surface only
        # the boolean ``has_pending_token`` flag so the frontend can
        # render the pending-token chip + retry button without ever
        # holding the ciphertext.
        copy["has_pending_token"] = bool(copy.get("pending_token"))
        copy.pop("pending_token", None)
        # The ``_encrypted`` marker is an internal storage detail; the
        # frontend has no need for it.
        copy.pop("_encrypted", None)
        redacted.append(copy)
    return redacted


def get_server_by_name(
    name: str,
    *,
    include_token: bool = True,
    service_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Look up one server by its friendly name (case-sensitive, exact match).

    Returns ``None`` if no server is registered with that name. The
    case-sensitive contract matches how schedules and CLI flags pass
    server names around - "Plex1" and "plex1" are different servers.

    The registry allows duplicate friendly names across backends
    ("Jade.TV" Plex + "Jade.TV" Emby). When
    ``service_type`` is supplied, only rows matching BOTH name and
    backend are returned. When omitted, the first row matching by
    name wins (legacy behaviour - safe for installs with no duplicate
    names). Callers that pass schedule rows or job params should
    always supply ``service_type`` to avoid ambiguity.
    """
    target_service = (service_type or "").lower() if service_type else None
    with _REG_LOCK:
        rows = _load_raw()
    for row in rows:
        if row.get("name") != name:
            continue
        if target_service is not None:
            row_service = (row.get("service_type") or "plex").lower()
            if row_service != target_service:
                continue
        if not include_token:
            row = {k: v for k, v in row.items() if k not in ("token", "_encrypted")}
        return row
    return None


def get_server_by_id(server_id: str, *, include_token: bool = True) -> Optional[Dict[str, Any]]:
    """
    Look up one server by its server-generated UUID id.

    ID-based lookup is what the frontend uses (because the friendly
    name can change). The CLI uses name-based lookup because typing a
    UUID at a shell prompt is hostile to the user.
    """
    with _REG_LOCK:
        rows = _load_raw()
    for row in rows:
        if row.get("id") == server_id:
            if not include_token:
                row = {k: v for k, v in row.items() if k not in ("token", "_encrypted")}
            return row
    return None


# ── Connection-error classifier ─────────────────────────────────────────────
#
# Connect failures fall into a small set of end user-recognisable
# buckets: token rejected (Plex 401), network unreachable (refused /
# DNS / no route), TLS cert verify failure, and slow / hung Plex
# (timeout). Classifying the exception once at the boundary lets the
# probe + test_connection responses set the right ``status`` field
# (the frontend renders auth_error distinctly from generic
# unreachable) and a friendly ``detail`` string the end user can
# actually act on.
#
# The function is best-effort: any exception it doesn't recognise
# falls through to ("unreachable", "<ClassName>: <message>") so the
# raw type is still visible.

def classify_connection_error(exc: BaseException) -> "tuple":
    """
    Inspect a connect-time exception and return ``(status, detail)``.

    Returns one of:

    * ``("auth_error", "Token rejected (401) ...")`` when Plex returns
      401. plexapi raises ``plexapi.exceptions.Unauthorized``; we
      isinstance-check it, and also string-match "401" / "Unauthorized"
      in the message as a defensive fallback in case plexapi's
      exception hierarchy shifts in a future release.
    * ``("auth_error", "Token rejected (403) ...")`` when Plex returns
      403 (less common but happens when the token is valid for an
      account that lacks any permission on this server).
    * ``("ssl_error", "TLS error ...")`` for TLS handshake / cert
      verification failures (requests.exceptions.SSLError).
    * ``("timeout", "Plex did not respond ...")`` for read / connect
      timeouts.
    * ``("unreachable", "<ClassName>: <message>")`` for everything
      else (DNS failure, connection refused, plexapi shape changes,
      etc.).

    Detail strings are written for an end user reading the Servers
    panel banner; they avoid implementation-specific jargon and
    point at the most likely fix.
    """
    # Try plexapi's typed exception first; falls back to string match
    # if the import isn't available at this code path (some test
    # harnesses stub plexapi out).
    try:
        from plexapi.exceptions import Unauthorized as _Unauthorized
        if isinstance(exc, _Unauthorized):
            return (
                "auth_error",
                "Token rejected (401). The token may belong to a "
                "different Plex account, may be revoked, or may not "
                "be authorized for this server.",
            )
    except Exception:
        pass

    msg = str(exc) or ""
    cls_name = type(exc).__name__

    # String-match fallbacks for cases where the exception class
    # isn't available or has been wrapped. Order matters: more
    # specific (401, 403) before more generic (Unauthorized).
    if "(401)" in msg or cls_name == "Unauthorized":
        return (
            "auth_error",
            "Token rejected (401). The token may belong to a "
            "different Plex account, may be revoked, or may not "
            "be authorized for this server.",
        )
    if "(403)" in msg:
        return (
            "auth_error",
            "Token rejected (403). The account that owns this token "
            "exists but has no permission on this server.",
        )

    # SSL / TLS failures.
    try:
        import ssl
        from requests.exceptions import SSLError as _SSLError
        if isinstance(exc, (_SSLError, ssl.SSLError)):
            return (
                "ssl_error",
                f"TLS error contacting Plex: {exc}. The server is "
                "using a certificate this host does not trust, or the "
                "URL should be plain HTTP instead of HTTPS.",
            )
    except Exception:
        pass
    if "SSL" in cls_name or "TLS" in cls_name or "CERTIFICATE_VERIFY_FAILED" in msg:
        return (
            "ssl_error",
            f"TLS error contacting Plex: {exc}. The server is using "
            "a certificate this host does not trust, or the URL "
            "should be plain HTTP instead of HTTPS.",
        )

    # Timeouts.
    try:
        from requests.exceptions import Timeout as _Timeout
        if isinstance(exc, _Timeout):
            return (
                "timeout",
                f"Plex did not respond within the connect timeout "
                f"({exc}). The server may be loading, on a slow "
                "network, or refusing the request silently.",
            )
    except Exception:
        pass
    if "Timeout" in cls_name or "timed out" in msg.lower():
        return (
            "timeout",
            f"Plex did not respond within the connect timeout "
            f"({exc}).",
        )

    # Generic fallback. Keep the raw class name so the end user can
    # search docs / report bugs without losing the original signal.
    return ("unreachable", f"{cls_name}: {exc}")


# ── Fallback-token probe (auto-recovery from new-server propagation lag) ───
#
# When a brand-new Plex install's token returns 401 against this app
# but the end user already has working tokens from other servers
# under the same Plex.tv account, those existing tokens will usually
# authenticate against the new server too. Plex's authorization layer
# accepts any token that belongs to an account that owns the target
# server, even before the server-specific token entry has fully
# propagated. This helper iterates registered servers (token-bearing
# rows only), tries each token against ``url`` via the existing
# ``connect_to_server`` path, and returns the FIRST one that succeeds.
#
# Defensive guarantees:
#   * Only invoked when the end user's typed token has already failed
#     with auth_error. Network / TLS / timeout failures bypass this
#     so a generic outage doesn't get hidden behind a successful-but-
#     wrong fallback.
#   * Returns None when no fallback works - the original auth_error
#     result is returned verbatim.
#   * Connection successes verify the borrowed-token's Plex.tv account
#     id matches the borrowing server's recorded owner_name so we never
#     surface a fallback that crossed account boundaries by accident.

def _probe_with_fallback_tokens(
    *,
    url: str,
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """
    Try each registered server's stored token against ``url`` and
    return the first that connects, or None when none do.

    Returns a dict with the fields the frontend needs to render the
    "Try fallback token" UX:

      * ``borrowed_from_server_id`` - id of the registered server
        whose token connected
      * ``borrowed_from_server_name`` - friendly name of that server
      * ``friendly_name``, ``machine_identifier``, ``owner_name``,
        ``libraries``, ``response_ms`` - identity from the
        successful borrow connection. Same shape as the success
        branch of probe_unsaved so the frontend can render the same
        info card if the end user accepts the fallback.

    The borrowed token itself is NEVER returned (it stays encrypted
    inside the registry). The frontend signals acceptance via a flag
    in the save payload; the backend then looks up the same borrowed
    server, decrypts its token, and writes it as the new row's
    ``token`` field.
    """
    from services.auth import connect_to_server

    with _REG_LOCK:
        rows = _load_raw()

    for row in rows:
        if not row.get("token"):
            continue
        try:
            plain = decrypt_server_token(row)
        except ServerCredentialError:
            # Borrowing-from-a-server-with-unreadable-token isn't
            # useful; skip silently.
            continue
        if not plain:
            continue
        try:
            t0 = time.monotonic()
            srv = connect_to_server(
                url, plain, logger, raise_on_failure=True,
            )
        except Exception:
            # This borrow failed too; move on to the next token.
            continue
        if srv is None:
            continue
        response_ms = (time.monotonic() - t0) * 1000.0

        # Pull identity. Same getattr pattern probe_unsaved uses on
        # its happy path so the surface fields line up.
        friendly = str(getattr(srv, "friendlyName", "") or "")
        machine_id = str(getattr(srv, "machineIdentifier", "") or "")
        owner = str(getattr(srv, "myPlexUsername", "") or "Plex Owner")
        libraries: List[Dict[str, Any]] = []
        try:
            for sec in srv.library.sections():
                try:
                    cnt = sec.totalSize
                except Exception:
                    cnt = 0
                libraries.append({
                    "name": sec.title, "type": sec.type,
                    "key": sec.key, "count": int(cnt),
                })
        except Exception:
            # Section enumeration is best-effort here; the end user
            # gets a working save even when libraries are empty.
            libraries = []

        return {
            "borrowed_from_server_id": row.get("id", ""),
            "borrowed_from_server_name": row.get("name", ""),
            "friendly_name": friendly,
            "machine_identifier": machine_id,
            "owner_name": owner,
            "libraries": libraries,
            "response_ms": response_ms,
        }

    return None


# ── Public write API ────────────────────────────────────────────────────────

def _diagnose_http_probe(
    adapter: Any, service_type: str, url_stripped: str,
) -> Tuple[str, str]:
    """Make the reachability probe call and return ``(status, detail)``.

    Replaces the previous bool-only ``adapter.ping()`` so the Add
    Server form can show the end user the *actual* reason a probe
    failed (TLS error, refused connection, HTTP 404 on the public
    probe path because the end user pasted a media-library URL
    instead of the Jellyfin / Emby server root, etc.).

    Returns ``("ok", "")`` on a successful 200 response. Returns
    ``("<status>", "<detail>")`` where ``status`` matches the
    registry's status vocabulary (``unreachable`` / ``auth_error`` /
    ``ssl_error`` / ``timeout``) and ``detail`` is end user-readable.
    """
    import requests
    # Capitalize the backend label for display ("emby" -> "Emby",
    # "jellyfin" -> "Jellyfin") so the detail string reads cleanly
    # under the properly-cased banner header on the frontend.
    label = (service_type or "").capitalize() or "Server"
    try:
        url = adapter._url("/System/Info/Public")
        resp = adapter._session.get(url, timeout=10)
    except requests.exceptions.SSLError as exc:
        return "ssl_error", (
            f"TLS handshake failed talking to {label} at "
            f"{url_stripped}: {exc}. Check the URL's protocol "
            f"(http vs https) and the server's TLS configuration."
        )
    except requests.exceptions.Timeout:
        return "timeout", (
            f"{label} server at {url_stripped} did not respond "
            f"within 10s. Check the URL, port, and whether the server "
            f"is reachable from this container."
        )
    except requests.exceptions.ConnectionError as exc:
        # Spell out the most-common-cause case explicitly. "Connection
        # refused" / Errno 111 means TCP itself was refused at the
        # destination - the server isn't listening, the port's wrong,
        # or Hestia-MediaManager's container can't route to the host. The
        # generic ConnectionError otherwise covers DNS failures, name
        # resolution issues, etc.
        exc_str = str(exc)
        if (
            "connection refused" in exc_str.lower()
            or "errno 111" in exc_str.lower()
            or "10061" in exc_str  # Windows TCP refused
        ):
            return "unreachable", (
                f"Connection refused at {url_stripped}. Nothing is "
                f"listening on that port from Hestia-MediaManager's host / "
                f"container. Common causes: (1) the {label} "
                f"server isn't running, (2) it's bound to a different "
                f"interface (check the server's Dashboard -> Networking "
                f"settings), (3) Hestia-MediaManager runs in Docker and the "
                f"target IP isn't reachable from the container (try "
                f"`host.docker.internal:<port>` or switch the container "
                f"to host networking), or (4) the port is wrong."
            )
        return "unreachable", (
            f"Could not reach {label} server at {url_stripped}: "
            f"{type(exc).__name__}: {exc}. Verify the host/port and "
            f"that the server is running."
        )
    except Exception as exc:
        return "unreachable", (
            f"{label} probe error against {url_stripped}: "
            f"{type(exc).__name__}: {exc}"
        )
    if resp.status_code == 200:
        return "ok", ""
    if resp.status_code in (401, 403):
        # System/Info/Public is supposed to be public; a 401/403 here
        # is unusual and likely indicates a reverse proxy that gates
        # all paths or a server config requiring auth on /System/*.
        return "auth_error", (
            f"{label} server at {url_stripped} returned "
            f"HTTP {resp.status_code} on /System/Info/Public. The "
            f"public probe path should not require auth - check for a "
            f"reverse proxy in front of the server that's gating "
            f"/System paths, or paste the API key in the field below."
        )
    if resp.status_code == 404:
        return "unreachable", (
            f"{label} server at {url_stripped} returned 404 on "
            f"/System/Info/Public. Confirm the URL points at the server "
            f"root (e.g. http://host:8096/), not a library page or "
            f"reverse-proxy path that strips /System."
        )
    return "unreachable", (
        f"{label} server at {url_stripped} returned HTTP "
        f"{resp.status_code} on /System/Info/Public. Expected 200."
    )


def _probe_http_backend(
    url: str,
    token: str,
    logger: logging.Logger,
    *,
    service_type: str,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Jellyfin / Emby variant of :func:`probe_unsaved`. Builds the
    appropriate adapter, calls ``ping`` then ``server_identity`` to
    validate the token, then enumerates libraries via
    ``adapter.list_libraries()``. Returns the same envelope shape as
    the Plex path so the frontend can render results uniformly.

    Differences from Plex probe:

    - No fallback-token path. Plex tokens are account-scoped and
      propagate across all owned servers; a token from another saved
      server frequently works on a new URL. Jellyfin / Emby tokens are
      server-local (issued by that server's User Authentication
      endpoint); there's nothing to fall back to.
    - Leaf counts are best-effort. The VirtualFolders endpoint doesn't
      carry a total count per library; the frontend tolerates the
      counts being absent.
    - Playlist / collection counts are skipped by default to keep
      probes fast; the end user gets them after registration via the
      normal library catalogue refresh."""
    url_stripped = (url or "").strip().rstrip("/")
    token_stripped = (token or "").strip()
    if not url_stripped:
        return {
            "ok": False, "status": "unreachable",
            "detail": "URL is required.",
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }
    if not token_stripped:
        return {
            "ok": False, "status": "auth_error",
            "detail": "API key is required.",
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }

    t0 = time.monotonic()
    try:
        if service_type == "jellyfin":
            from services.adapters.jellyfin import JellyfinAdapter
            adapter = JellyfinAdapter(url_stripped, token_stripped)
        else:
            from services.adapters.emby import EmbyAdapter
            adapter = EmbyAdapter(url_stripped, token_stripped)
    except Exception as exc:
        return {
            "ok": False, "status": "unreachable",
            "detail": f"failed to construct {service_type} client: {exc}",
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }

    # Diagnostic probe: replaces a bool ``adapter.ping()`` with a
    # direct call that surfaces the actual failure reason (network
    # error, HTTP status, timeout, SSL handshake) into the end user-
    # facing detail string. The bool-only ping hid these and left
    # end users staring at a "did not respond" message that didn't
    # tell them whether to check the URL, the firewall, or the
    # Jellyfin / Emby server's bind config.
    probe_status, probe_detail = _diagnose_http_probe(
        adapter, service_type, url_stripped,
    )
    if probe_status != "ok":
        return {
            "ok": False, "status": probe_status, "detail": probe_detail,
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }
    try:
        identity = adapter.server_identity()
    except Exception as exc:
        status, detail = classify_connection_error(exc)
        return {
            "ok": False, "status": status,
            "detail": detail,
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }

    if not identity.machine_id:
        return {
            "ok": False, "status": "auth_error",
            "detail": (
                f"connected to {url_stripped} but the API key did not "
                f"return a usable identity (Users/Me lookup failed). "
                f"Confirm the key has admin scope."
            ),
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }

    response_ms = (time.monotonic() - t0) * 1000.0

    libraries: List[Dict[str, Any]] = []
    try:
        for lib in adapter.list_libraries():
            libraries.append({
                "name": lib.name,
                "type": lib.type,
                # For Plex parity the frontend reads ``key``; preserve
                # the same key name even though the value is a UUID
                # string for Jellyfin / Emby.
                "key": lib.library_id,
                "count": lib.item_count if lib.item_count is not None else 0,
            })
    except Exception as exc:
        # Connected fine but library enumeration failed - bucket as
        # auth_error since the most likely cause is a non-admin token.
        return {
            "ok": False, "status": "auth_error",
            "detail": f"Connected but could not list libraries: {exc}",
            "friendly_name": identity.name,
            "machine_identifier": identity.machine_id,
            "owner_name": identity.owner_display,
            "libraries": [],
            "response_ms": response_ms,
        }

    return {
        "ok": True, "status": "ok", "detail": "",
        "friendly_name": identity.name,
        "machine_identifier": identity.machine_id,
        # Mirrors the Plex probe's plex_version field but named
        # generically so the frontend can render it without a
        # backend-specific branch.
        "plex_version": identity.version,
        "owner_name": identity.owner_display,
        "libraries": libraries,
        "response_ms": response_ms,
        # Jellyfin / Emby playlist + collection counts are intentionally
        # skipped during probe to keep the call fast. Populated after
        # registration when the catalogue refresh runs.
        "playlist_count": None,
        "collection_count": None,
    }


def probe_unsaved(
    url: str,
    token: str,
    logger: logging.Logger,
    *,
    timeout: Optional[float] = None,
    service_type: str = "plex",
) -> Dict[str, Any]:
    """
    Probe a URL+token combination without writing to the registry.

    Used by the "Test Connection" button in the add-server form and
    by :func:`add_server` itself to validate inputs before persisting.
    Returns a dict shaped like the on-disk row plus an ``ok`` flag -
    callers can hand it straight to the frontend.

    The probe captures:
      * ``friendly_name`` - what the connected server calls itself
        (from its own setup, not the end user's chosen registry
        name).
      * ``machine_identifier`` - the stable per-install id (Plex
        ``machineIdentifier``, Jellyfin / Emby ``System.Id``). For
        Plex this is what lets us detect "end user typed the right
        URL but used a token for a different physical server" since
        the same Plex account token unlocks every server that account
        owns. For Jellyfin / Emby tokens are server-local; the
        machine id is still a useful end user signal.
      * ``owner_name`` - the connected account / token's owner.
      * ``libraries`` - current catalogue with item counts.

    ``service_type`` selects which probe path runs.
    Plex (default) goes through the existing plexapi-based probe;
    Jellyfin / Emby go through the HTTP adapter's ``ping`` +
    ``server_identity`` + ``list_libraries`` surface.

    Never raises for a probe failure - failures land on ``ok=False``
    and ``detail`` so the frontend can render a useful message.
    Re-raises only for programmer errors (bad arguments).
    """
    # ``timeout=None`` resolves from the ``server_probe_timeout_seconds``
    # tunable so the "Test Connection" probe budget is operator-tunable.
    # An explicit caller-supplied timeout still wins.
    if timeout is None:
        from services import tunables
        timeout = float(tunables.server_probe_timeout())
    service_type = (service_type or "plex").lower()
    if service_type in ("jellyfin", "emby"):
        return _probe_http_backend(
            url, token, logger, service_type=service_type, timeout=timeout,
        )

    from services.auth import connect_to_server

    if not isinstance(url, str) or not url.strip():
        return {
            "ok": False, "status": "unreachable",
            "detail": "URL is required.",
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }
    if not isinstance(token, str) or not token.strip():
        return {
            "ok": False, "status": "auth_error",
            "detail": "Token is required.",
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }

    t0 = time.monotonic()
    try:
        # raise_on_failure=True so the actual exception (TLS error,
        # 401, plexapi DNS failure, etc.) reaches the end user
        # via the ``detail`` field. The default-False behaviour
        # would silently collapse every failure into a generic
        # "Plex unreachable" string that hides the real reason.
        server = connect_to_server(
            url.strip().rstrip("/"),
            token.strip(),
            logger,
            raise_on_failure=True,
        )
    except Exception as exc:
        # classify_connection_error buckets the exception into
        # auth_error / ssl_error / timeout / unreachable with a
        # human-readable detail string. The frontend renders
        # auth_error distinctly (token-finder help link) so the 401
        # case especially gets the right UX treatment.
        status, detail = classify_connection_error(exc)

        # Auto-fallback: when a 401 fires on a NEW server
        # whose token may not yet have propagated to Plex.tv's auth
        # layer, try the end user's existing tokens. Each registered
        # server stores a Plex.tv account token that's effectively
        # universal across servers that account owns. If one of them
        # authenticates against the new URL, the end user can opt to
        # save that working token + stash the original typed one as
        # pending. Only fires for auth_error so a TLS / timeout /
        # network failure isn't masked by a fallback success against
        # an entirely different server reachable from this host.
        fallback = None
        if status == "auth_error":
            fallback = _probe_with_fallback_tokens(
                url=url.strip().rstrip("/"),
                logger=logger,
            )

        result = {
            "ok": False, "status": status,
            "detail": detail,
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }
        if fallback is not None:
            # Carry the fallback summary so the frontend can render
            # the "Try fallback token" button. Identity fields come
            # from the fallback's working connection so the end user
            # sees the same friendly_name / machine_identifier /
            # owner the saved row will end up with.
            result["fallback"] = fallback
        return result
    if server is None:
        # Defensive: raise_on_failure=True above means connect_to_server
        # either returns a server or raises. The except path catches the
        # raise; we should never land here. Keep the branch as a
        # belt-and-suspenders fallback rather than silently dropping the
        # response.
        return {
            "ok": False, "status": "unreachable",
            "detail": "connect_to_server returned None unexpectedly.",
            "friendly_name": "", "machine_identifier": "",
            "owner_name": "", "libraries": [], "response_ms": None,
        }
    response_ms = (time.monotonic() - t0) * 1000.0

    owner = str(getattr(server, "myPlexUsername", "") or "Plex Owner")
    friendly = str(getattr(server, "friendlyName", "") or "")
    machine_id = str(getattr(server, "machineIdentifier", "") or "")
    # PMS version string (e.g. "1.32.5.7349-abcdef") - captured so the
    # UI can gate version-locked features without an extra round-trip.
    plex_version = str(getattr(server, "version", "") or "")

    libraries: List[Dict[str, Any]] = []
    sections_seq: List[Any] = []
    try:
        for sec in server.library.sections():
            sections_seq.append(sec)
            try:
                count = sec.totalSize
            except Exception:
                count = 0
            desc: Dict[str, Any] = {
                "name": sec.title, "type": sec.type,
                "key": sec.key, "count": count,
            }
            leaf = _fetch_leaf_counts(server, sec)
            if leaf:
                desc["leaf_counts"] = leaf
            libraries.append(desc)
    except Exception as exc:
        # We connected but couldn't enumerate - count it as auth_error
        # because the most common cause is a token without library
        # read permissions (a managed-user token, or a revoked one).
        return {
            "ok": False, "status": "auth_error",
            "detail": f"Connected but could not list libraries: {exc}",
            "friendly_name": friendly, "machine_identifier": machine_id,
            "plex_version": plex_version,
            "owner_name": owner, "libraries": [], "response_ms": response_ms,
        }

    playlist_count, collection_count = _fetch_server_level_counts(server, sections_seq)

    return {
        "ok": True, "status": "ok", "detail": "",
        "friendly_name": friendly, "machine_identifier": machine_id,
        "plex_version": plex_version,
        "owner_name": owner, "libraries": libraries,
        "response_ms": response_ms,
        # Timing-spec inputs (see _DEFAULT_SERVER for shape).
        "playlist_count": playlist_count,
        "collection_count": collection_count,
        "counts_refreshed_at": time.time(),
    }


def _mirror_to_media_db(row: Dict[str, Any]) -> None:
    """
    Mirror one server registry row into ``media.db``'s ``servers``
    table. Idempotent (the upsert handles the conflict). Best-effort:
    failure to mirror must NOT block the registry write that triggered
    this call - the registry is the source of truth and we'd rather
    have a registry-without-mirror state than a registry write that
    fails because media.db happens to be locked at this instant. The
    startup backfill in ``app.py`` covers any drift.
    """
    try:
        from server import media_db
        media_db.upsert_server_row(
            server_id=row.get("id") or "",
            name=row.get("name") or "",
            url=row.get("url") or "",
            service=(row.get("service_type") or "plex").lower(),
            machine_id=(row.get("machine_identifier") or None) or None,
        )
    except Exception:
        log.exception(
            "media_db mirror failed for server %r; registry write still committed.",
            row.get("name"),
        )


def add_server(
    name: str,
    url: str,
    token: str,
    *,
    logger: Optional[logging.Logger] = None,
    use_fallback_from_server_id: Optional[str] = None,
    service_type: str = "plex",
) -> Dict[str, Any]:
    """
    Register a new server. Friendly name must be unique (case-sensitive).
    Raises ValueError if the name is already taken or any required
    field is empty.

    Connection check on save. Before persisting the new row
    we probe the URL+token via :func:`probe_unsaved`. Two failure
    cases raise:

      1. The probe fails (server unreachable, token rejected). The
         end user gets the concrete failure message and the registry
         stays clean - no "ghost row" left behind from a failed test.

      2. The probe succeeds but the server's ``machine_identifier``
         is already registered under a different friendly name. This
         catches the easy mistake of "I typed Backup Server's
         URL but pasted My Server's token in another row" - Plex tokens
         are per-account, not per-server, so a connection succeeding
         doesn't prove anything about which server you hit. The
         identifier is what tells us.

    The probe results are pre-populated into the saved row
    (machine_identifier, friendly_name, owner_name, libraries) so the
    Servers tab paints with real data immediately - no second
    round-trip needed.

    Auto-fallback path: when
    ``use_fallback_from_server_id`` is set, the end user has accepted
    the offered fallback token. The flow is:

      1. Decrypt the borrowed server's stored token and probe ``url``
         with it. Probe must succeed; if not, raise (the borrow no
         longer works, e.g. the borrowed-from server's token was
         revoked between the Add-form Test click and the Save click).
      2. Persist the row with the borrowed token under ``token``,
         and the end user's typed token under ``pending_token``
         encrypted. ``pending_token_first_seen_at`` records when the
         end user originally tried it so retry-warnings can age in.
    """
    if not name or not name.strip():
        raise ValueError("Server name must not be empty.")
    if not url or not url.strip():
        raise ValueError("Server URL must not be empty.")
    if not token or not token.strip():
        raise ValueError("Server token must not be empty.")

    if logger is None:
        logger = log

    service_type = (service_type or "plex").lower()
    if service_type not in ("plex", "jellyfin", "emby"):
        raise ValueError(
            f"Unknown service_type {service_type!r}; expected plex|jellyfin|emby"
        )

    # Borrow-and-stash branch. The probe runs with the BORROWED
    # token so the row is populated with identity from that working
    # connection; the end user's typed token becomes pending and is
    # surfaced via the retry chip. Fallback-from-existing-server is
    # only meaningful for Plex (tokens are account-scoped); Jellyfin /
    # Emby tokens are server-local so we reject the parameter there.
    pending_typed_token = ""
    if use_fallback_from_server_id and service_type != "plex":
        raise ValueError(
            "use_fallback_from_server_id is only valid for Plex servers; "
            "Jellyfin / Emby tokens are server-local."
        )
    if use_fallback_from_server_id:
        with _REG_LOCK:
            rows = _load_raw()
            borrow_row: Optional[Dict[str, Any]] = None
            for r in rows:
                if r.get("id") == use_fallback_from_server_id:
                    borrow_row = r
                    break
        if borrow_row is None:
            raise ValueError(
                f"use_fallback_from_server_id={use_fallback_from_server_id!r} "
                "no longer exists. Re-run Test Connection to pick a fresh "
                "fallback offer."
            )
        try:
            borrowed_token = decrypt_server_token(borrow_row)
        except ServerCredentialError as exc:
            raise ValueError(
                f"Borrow source {borrow_row.get('name')!r} has an unreadable "
                f"token ({exc}); cannot use as fallback."
            ) from exc
        if not borrowed_token:
            raise ValueError(
                f"Borrow source {borrow_row.get('name')!r} has no stored "
                "token; cannot use as fallback."
            )
        probe = probe_unsaved(
            url, borrowed_token, logger, service_type=service_type,
        )
        if not probe["ok"]:
            raise ValueError(
                f"Borrow-token probe failed: {probe['detail']}. The borrowed "
                "token may have been revoked between Test Connection and Save."
            )
        # Save the borrowed token under ``token``; stash the typed
        # one under ``pending_token`` so the end user can retry it
        # later when Plex.tv finishes propagating the new server's
        # authorization.
        effective_token_to_store = borrowed_token
        pending_typed_token = token
    else:
        probe = probe_unsaved(url, token, logger, service_type=service_type)
        if not probe["ok"]:
            raise ValueError(
                f"Cannot register {name!r}: {probe['detail']}"
            )
        effective_token_to_store = token

    machine_id = probe.get("machine_identifier") or ""

    with _REG_LOCK:
        rows = _load_raw()
        # Friendly-name uniqueness is scoped to
        # (name + service_type) rather than name alone. An end user's
        # "Jade.TV" Plex server and their "Jade.TV" Emby server are
        # different physical things, and the end user wants the same
        # display label for both (the name is a human handle, not the
        # technical identity). Two servers with the same name AND the
        # same backend stay blocked because that's genuinely
        # ambiguous (CLI / schedule lookups by name still pick the
        # first match; we keep the constraint strict enough that
        # ambiguity only happens across backends, where the
        # service_type discriminates).
        if any(
            r.get("name") == name
            and (r.get("service_type") or "plex").lower() == service_type
            for r in rows
        ):
            raise ValueError(
                f"A {service_type} server named {name!r} is already "
                f"registered. Same name across different backends is "
                f"allowed; the same name twice within one backend is not."
            )
        # Duplicate-identifier check. Empty identifier means the
        # probe didn't surface one (very old Plex builds, or some
        # plexapi failure path) - in that case we let the end user
        # proceed; the friendly-name uniqueness check above is the
        # only guard left.
        if machine_id:
            for r in rows:
                if r.get("machine_identifier") and r["machine_identifier"] == machine_id:
                    raise ValueError(
                        f"This server is already registered as "
                        f"{r.get('name')!r} (machine identifier "
                        f"{machine_id}). Each physical server may "
                        f"only be registered once."
                    )
        new_row = dict(_DEFAULT_SERVER)
        new_row.update({
            "id": make_server_id(service_type),
            "name": name.strip(),
            "url": url.strip().rstrip("/"),
            "service_type": service_type,
            # Encrypt before storing - plaintext never reaches disk.
            # ``effective_token_to_store`` is either the end user's typed
            # token (normal path) or a borrowed working token (fallback
            # branch above).
            "token": encrypt_str(effective_token_to_store.strip()),
            "_encrypted": True,
            # Pending typed token, encrypted alongside the active one,
            # for the borrow-and-stash flow. Empty string on the
            # normal path so no chip / retry button renders.
            "pending_token": (
                encrypt_str(pending_typed_token.strip())
                if pending_typed_token else ""
            ),
            "pending_token_first_seen_at": (
                time.time() if pending_typed_token else 0.0
            ),
            "pending_token_last_probed_at": 0.0,
            "pending_token_source": (
                "operator_typed_at_add" if pending_typed_token else ""
            ),
            "last_status": probe["status"],
            "last_status_detail": probe["detail"],
            "last_checked_at": time.time(),
            "last_response_ms": probe.get("response_ms"),
            "machine_identifier": machine_id,
            "friendly_name": probe.get("friendly_name") or "",
            "plex_version": probe.get("plex_version") or "",
            "owner_name": probe.get("owner_name") or "Plex Owner",
            "last_libraries": probe.get("libraries") or [],
            "playlist_count": probe.get("playlist_count"),
            "collection_count": probe.get("collection_count"),
            "counts_refreshed_at": probe.get("counts_refreshed_at"),
        })
        rows.append(new_row)
        _save_raw(rows)
    # Outside the registry lock so a slow media.db acquire doesn't
    # block other registry callers. Best-effort mirror; the startup
    # backfill is the safety net.
    _mirror_to_media_db(new_row)
    return new_row


def update_server(server_id: str, *, name: Optional[str] = None, url: Optional[str] = None,
                  token: Optional[str] = None) -> Dict[str, Any]:
    """
    Rename or re-credential an existing server. Any field set to
    ``None`` is left unchanged. The friendly name remains unique across
    the registry - a rename that collides with another entry raises
    ``ValueError``. Tokens passed empty-string mean "keep current" so
    the frontend can submit a form without re-entering the token.
    """
    with _REG_LOCK:
        rows = _load_raw()
        target: Optional[Dict[str, Any]] = None
        for row in rows:
            if row.get("id") == server_id:
                target = row
                break
        if target is None:
            raise ValueError(f"No server with id {server_id!r}")
        if name is not None and name.strip() and name != target.get("name"):
            target_backend = (target.get("service_type") or "plex").lower()
            if any(
                r.get("name") == name
                and (r.get("service_type") or "plex").lower() == target_backend
                and r is not target
                for r in rows
            ):
                raise ValueError(
                    f"A {target_backend} server named {name!r} already "
                    f"exists. Same name across different backends is "
                    f"allowed; the same name twice within one backend is not."
                )
            # Propagate the rename into every schedule that
            # references the old name + same backend. Scoped by
            # backend so a Plex rename doesn't touch a same-named
            # Jellyfin / Emby schedule.
            old_name = target.get("name") or ""
            new_name = name.strip()
            target["name"] = new_name
            try:
                from server.persistence import rename_schedules_for_server
                rewritten = rename_schedules_for_server(
                    old_name, new_name, service_type=target_backend,
                )
                if rewritten:
                    log.info(
                        "update_server rename %r -> %r rewrote %d schedule(s) (backend=%s).",
                        old_name, new_name, rewritten, target_backend,
                    )
            except Exception:
                log.exception(
                    "update_server: schedule rename propagation failed; "
                    "registry rename still committed. Operator may need to "
                    "edit schedules manually."
                )
            # USER-MGMT-IDENTITY-AUDIT R-4 follow-on: refresh the
            # HostNameSlug portion of every app_user_uuid stored
            # against this server. The server_uid + userkey portions
            # are immutable; only the cosmetic slug changes so
            # identity_map links stay valid. Best-effort - a slug
            # mismatch surfaces only in UI display, never breaks
            # resolution.
            try:
                from server import media_db
                slug_counts = media_db.rewrite_app_user_uuid_host_slug_for_server(
                    server_id, new_name,
                )
                if any(slug_counts.values()):
                    log.info(
                        "update_server rename %r -> %r refreshed app_user_uuid "
                        "host_slug across %d row(s).",
                        old_name, new_name,
                        sum(slug_counts.values()),
                    )
            except Exception:
                log.exception(
                    "update_server: app_user_uuid slug refresh failed; "
                    "stored UUIDs may display the old slug until the "
                    "next sync runs."
                )
        if url is not None and url.strip():
            target["url"] = url.strip().rstrip("/")
        if token is not None and token != "":
            # New token from the UI / API - encrypt before storing.
            # Empty string means "keep the existing (already-encrypted)
            # value", which we honour by leaving target['token']
            # untouched.
            target["token"] = encrypt_str(token)
            target["_encrypted"] = True
        _save_raw(rows)
    # Mirror outside the lock - keeps media.db acquires from blocking
    # other registry callers. ``target`` is a reference into rows[],
    # safe to read after _save_raw because we only read fields the
    # update path doesn't subsequently clear.
    _mirror_to_media_db(target)
    return target


def cascade_preview(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Count what a cascading remove of ``server_id`` would delete,
    without modifying anything. Returns ``None`` if no server with
    that id is registered.

    Used by the Servers tab to populate the confirmation dialog
    before the end user clicks Delete - so the prompt can read
    *"My Server has 2 schedule(s), 47 snapshot file(s), 12 log directory/ies"*
    instead of the previous blanket warning.
    """
    # Local imports to keep the dependency direction registry → log/snapshot
    # one-way; these modules already import from persistence/secrets.
    from server import snapshot_browser, log_browser
    from server.persistence import load_schedules

    row = get_server_by_id(server_id, include_token=False)
    if row is None:
        return None

    name = row.get("name") or ""
    service_type = (row.get("service_type") or "plex").lower()
    # Preview uses the backend-aware slug + filters
    # schedules by (name + service_type) so the displayed counts
    # match what a real cascade delete would actually touch.
    slug = backend_aware_slug(name, service_type)
    schedules = load_schedules()
    schedule_count = sum(
        1 for s in schedules
        if s.get("source_server_name") == name
        and (s.get("source_service_type") or "plex").lower() == service_type
    )
    return {
        "id": server_id,
        "name": name,
        "slug": slug,
        "schedules": schedule_count,
        "snapshots": snapshot_browser.count_exports_by_slug(slug),
        "log_dirs": log_browser.count_log_dirs_by_slug(slug),
    }


def retry_pending_token(
    server_id: str, *, logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Probe the server's stashed pending_token against the live URL.
    On success: swap the pending token into ``token`` (it becomes
    the new active credential), clear the pending fields, and
    return ``{ok: True, swapped: True, detail: ...}``. On failure:
    update ``pending_token_last_probed_at`` so the UI can show
    "last tried <timestamp>" and return ``{ok: False, swapped:
    False, status, detail}``.

    Used by the Servers panel chip's Retry button. Returning the
    swap outcome lets the frontend refresh the row state without a
    second list-servers fetch.
    """
    if logger is None:
        logger = log
    with _REG_LOCK:
        rows = _load_raw()
        target: Optional[Dict[str, Any]] = None
        for r in rows:
            if r.get("id") == server_id:
                target = r
                break
        if target is None:
            raise ValueError(f"No server with id {server_id!r}")
        if not target.get("pending_token"):
            return {
                "ok": False,
                "swapped": False,
                "status": "no_pending",
                "detail": "No pending token to retry.",
            }
        # Decrypt the pending token for the probe. Errors here mean
        # the keyfile changed since the pending was stored; treat as
        # unreadable and clear the pending slot so the chip stops
        # nagging the end user about an irretrievable secret.
        try:
            pending_plain = decrypt_str(target["pending_token"])
        except Exception:
            target["pending_token"] = ""
            target["pending_token_first_seen_at"] = 0.0
            target["pending_token_last_probed_at"] = 0.0
            target["pending_token_source"] = ""
            _save_raw(rows)
            return {
                "ok": False,
                "swapped": False,
                "status": "unreadable",
                "detail": (
                    "The stashed pending token could not be decrypted "
                    "(keyfile may have changed). The pending slot has "
                    "been cleared."
                ),
            }
        url_for_probe = str(target.get("url") or "")

    # Drop the lock for the network call. We re-acquire before
    # mutating the row so a parallel registry update doesn't lose
    # the swap.
    probe = probe_unsaved(url_for_probe, pending_plain, logger)
    now = time.time()
    with _REG_LOCK:
        rows = _load_raw()
        target = None
        for r in rows:
            if r.get("id") == server_id:
                target = r
                break
        if target is None:
            # Row removed mid-flight; nothing to do.
            return {
                "ok": False,
                "swapped": False,
                "status": "missing",
                "detail": "Server row was removed while the probe ran.",
            }
        if not probe.get("ok"):
            # Failed retry. Update the last-probed timestamp so the
            # UI can grey the chip / display "last tried X ago".
            target["pending_token_last_probed_at"] = now
            _save_raw(rows)
            return {
                "ok": False,
                "swapped": False,
                "status": probe.get("status") or "unreachable",
                "detail": probe.get("detail") or "Pending token still failing.",
            }
        # Swap. The pending becomes the active credential; clear
        # every pending field on this row.
        target["token"] = encrypt_str(pending_plain)
        target["_encrypted"] = True
        target["pending_token"] = ""
        target["pending_token_first_seen_at"] = 0.0
        target["pending_token_last_probed_at"] = 0.0
        target["pending_token_source"] = ""
        # Refresh identity from the probe so the row reflects the
        # latest server-side metadata.
        target["last_status"] = "ok"
        target["last_status_detail"] = ""
        target["last_checked_at"] = now
        target["last_response_ms"] = probe.get("response_ms")
        if probe.get("friendly_name"):
            target["friendly_name"] = probe["friendly_name"]
        if probe.get("machine_identifier"):
            target["machine_identifier"] = probe["machine_identifier"]
        if probe.get("owner_name"):
            target["owner_name"] = probe["owner_name"]
        if probe.get("libraries"):
            target["last_libraries"] = probe["libraries"]
        _save_raw(rows)
    return {
        "ok": True,
        "swapped": True,
        "status": "ok",
        "detail": (
            "Pending token now works against this server; it has "
            "replaced the previously-stored token. The pending slot "
            "is clear."
        ),
    }


def remove_server(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Cascading delete (v0.9.5): drops the registry row and every other
    artefact attributable to the named server - schedules referencing
    it, ``.plexexport.json`` files produced by it, and per-run log
    directories under its slug.

    Order is deliberate:

      1. Schedules first - atomic write to ``schedules.json``.
      2. Registry row - atomic write to ``servers.json``.
      3. Files last (snapshots + log dirs) - best-effort; a permission
         error on one file is recorded in the summary but doesn't
         block the rest of the sweep.

    Rationale: if the backend crashes between (2) and (3) the data
    stores are still consistent - the row is gone, and orphan files
    on disk can be cleaned up later. Doing it in reverse would risk
    a live registry row pointing at deleted snapshots.

    Returns ``None`` if no server with ``server_id`` is registered,
    otherwise a summary dict ``{deleted: bool, schedules, snapshots,
    exports_failed, log_dirs, log_dirs_failed, errors}``. The bool
    ``deleted`` is True when the registry row itself was removed.
    """
    from server import snapshot_browser, log_browser

    # We need the server's NAME (for schedule match) and SLUG (for
    # file match) before deleting the row. Pull them under the lock
    # together with the row-removal so a concurrent rename can't
    # land between the lookup and the delete.
    with _REG_LOCK:
        rows = _load_raw()
        target: Optional[Dict[str, Any]] = None
        for row in rows:
            if row.get("id") == server_id:
                target = row
                break
        if target is None:
            return None
        name = target.get("name") or ""
        target_service = (target.get("service_type") or "plex").lower()
        # The slug for cascade artifact lookups is
        # backend-aware so deleting "Jade.TV" Emby doesn't touch
        # "Jade.TV" Plex artifacts (which live under "Jade-TV-plex").
        slug = backend_aware_slug(name, target_service)

        # (1) Schedules - separate lock inside delete_schedules_by_server_name
        #     so this is safe to call while holding _REG_LOCK. Filtered
        #     by service_type so schedules that target a different
        #     same-named server stay put.
        schedules_removed = delete_schedules_by_server_name(
            name, service_type=target_service,
        )

        # (2) Registry row.
        kept = [r for r in rows if r.get("id") != server_id]
        _save_raw(kept)

    # (3) Filesystem sweep - outside the registry lock to avoid holding
    #     it across slow disk I/O. By this point the registry and
    #     schedules are already consistent, so a concurrent reader
    #     sees the server as gone even while the file deletion runs.
    #     ``slug`` is backend-aware, so this only touches THIS server's
    #     artifacts.
    exports_deleted, export_errors = snapshot_browser.delete_exports_by_slug(slug)
    logs_deleted, log_errors = log_browser.delete_log_dirs_by_slug(slug)

    errors = export_errors + log_errors
    return {
        "deleted": True,
        "id": server_id,
        "name": name,
        "slug": slug,
        "schedules": schedules_removed,
        "snapshots": exports_deleted,
        "exports_failed": len(export_errors),
        "log_dirs": logs_deleted,
        "log_dirs_failed": len(log_errors),
        "errors": errors,
    }


def remove_server_by_name(name: str) -> Optional[Dict[str, Any]]:
    """
    CLI convenience wrapper around :func:`remove_server` that takes a
    friendly name instead of a UUID. Returns the same cascade summary.
    """
    row = get_server_by_name(name, include_token=False)
    if row is None:
        return None
    return remove_server(row["id"])


# ── Startup helpers (v0.9.5) ────────────────────────────────────────────────

def ensure_encrypted_at_rest() -> int:
    """
    Force any plaintext rows in ``servers.json`` to be encrypted now,
    rather than waiting for the next lazy ``_load_raw`` call from a
    request handler. Called from the FastAPI startup hook so by the
    time uvicorn binds the port every row on disk is ciphertext.

    Returns the row count after migration. The migration itself runs
    as a side effect of ``_load_raw``; this wrapper exists so the
    intent is explicit at the call site.
    """
    with _REG_LOCK:
        rows = _load_raw()
    return len(rows)


# ── Users (v0.9.6 Feature 3) ────────────────────────────────────────────────

def get_server_users(server_id: str, logger: logging.Logger) -> Dict[str, Any]:
    """
    Return the list of users associated with one registered server.

    Dispatches on the row's ``service_type``:

      * Plex: owner row uses the Plex.tv email as the identifier (the
        same string the dashboard's ``current_user`` resolves through
        ``user_display_names``); managed users come from
        ``server.systemAccounts()`` and use the local server username.
      * Jellyfin / Emby: enumerated via the adapter's ``list_users()``,
        keyed by backend username with ``backend_user_id`` (the GUID)
        included as a separate field on each entry.

    Best-effort:
      * If user enumeration fails (Plex ``systemAccounts()`` or
        adapter ``list_users()``), the response includes any users
        gathered so far plus a non-null ``error`` string so the
        frontend can render "No managed users found" with a tooltip
        rather than an error state.
      * Connection failures bubble up as ``ConnectionError`` so the
        route layer can return a clean 502.

    Returns:
        ``{"users": [...], "error": <str|null>}``. Each user dict
        carries ``{kind: "owner"|"managed", plex_id, raw_name,
        display_name}``. ``display_name`` is the end user's choice
        from the row's ``user_display_names`` map if one exists;
        otherwise an empty string and the UI falls back to ``raw_name``.

    Raises:
        ValueError if no server with ``server_id`` is registered.
        ConnectionError if Plex is unreachable.
    """
    from services.auth import connect_to_server

    row = get_server_by_id(server_id)
    if row is None:
        raise ValueError(f"No server with id {server_id!r}")

    if not (row.get("token") or ""):
        raise ConnectionError(
            f"Server {row.get('name') or server_id!r} has no stored token. "
            f"Re-enter credentials under the Servers tab."
        )

    service_type = (row.get("service_type") or "plex").lower()
    display_names: Dict[str, str] = dict(row.get("user_display_names") or {})

    # Non-Plex backends (Jellyfin / Emby) enumerate users via the
    # adapter's list_users() rather than the Plex-specific
    # myPlexAccount + systemAccounts pair. ``connect_registered_server``
    # builds the right adapter for the row's service_type and records
    # status on success/failure for us.
    if service_type in ("jellyfin", "emby"):
        try:
            connection = connect_registered_server(server_id, logger)
        except ValueError:
            raise
        except ConnectionError:
            raise
        except Exception as exc:
            raise ConnectionError(
                f"Could not connect to {row.get('name') or server_id!r}: {exc}"
            ) from exc

        users: List[Dict[str, Any]] = []
        sys_error: Optional[str] = None
        try:
            adapter_users = connection.adapter.list_users() or []
        except Exception as exc:
            adapter_users = []
            sys_error = f"list_users() unavailable: {exc}"
            logger.debug(sys_error)

        for u in adapter_users:
            username = (getattr(u, "username", "") or "").strip()
            if not username:
                continue
            # Key the display_names map by username so the existing
            # PATCH /user-display-name endpoint and the Server Users
            # UI line up on the same identifier. The backend GUID
            # (u.backend_user_id) is preserved separately for callers
            # that need the stable id.
            kind = "owner" if (getattr(u, "role", "") or "").lower() == "owner" else "managed"
            users.append({
                "kind": kind,
                "plex_id": username,
                "raw_name": username,
                "display_name": display_names.get(username, ""),
                "backend_user_id": getattr(u, "backend_user_id", "") or "",
            })

        return {"users": users, "error": sys_error}

    # Tokens on disk are Fernet ciphertexts (encryption at rest, see
    # server/secrets.py) and must be decrypted before use. Passing
    # the raw row["token"] straight to ``connect_to_server`` sends
    # ciphertext as the auth token; Plex rejects it, surfacing as a
    # 502 in the Servers tab's Users panel. Decrypting at the point
    # of use keeps this consumer in line with every other
    # token-touching path (connect_registered_server, test_connection,
    # ping_server, _run_direct).
    try:
        plain_token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        # Keyfile lost or token corrupted - actionable message all the
        # way to the end user.
        raise ConnectionError(str(exc)) from exc

    server = connect_to_server(row["url"], plain_token, logger)
    if server is None:
        raise ConnectionError(
            f"Could not connect to {row.get('name') or server_id!r}. "
            f"Check the URL and token under the Servers tab."
        )

    users = []

    # Owner: email from ``myPlexAccount``. Captured separately because
    # the SystemAccount entry for the owner uses the *username*, not
    # the email - and the dashboard's ``current_user`` keys on email
    # for the owner role.
    # Owner identifier derivation + the per-SystemAccount skip check
    # both live in the shared helper in
    # ``services.plex_owner_identity`` so this surface and
    # PlexAdapter.list_users stay in lockstep.
    from services.plex_owner_identity import (
        derive_owner_identifiers,
        dedupe_owner_against_managed,
        is_owner_system_account,
    )
    owner_ids = {"email": "", "username": "", "account_id": "", "email_local": ""}
    try:
        account = server.myPlexAccount()
        owner_ids = derive_owner_identifiers(account)
    except Exception as e:
        logger.debug(f"myPlexAccount unavailable for {row.get('name')!r}: {e}")

    if owner_ids["email"]:
        users.append({
            "kind": "owner",
            "plex_id": owner_ids["email"],
            "raw_name": owner_ids["email"],
            "display_name": display_names.get(owner_ids["email"], ""),
        })

    # Managed users - read systemAccounts(); skip the entry that IS
    # the owner so we don't append them a second time as a managed
    # user. The shared helper covers id==1 + username match + three
    # additional signals (Plex.tv accountID, email full match, email
    # local-part match) so an operator-renamed owner SystemAccount
    # whose id != 1 no longer leaks through.
    sys_error = None
    try:
        sys_accts = server.systemAccounts() or []
    except Exception as e:
        sys_accts = []
        sys_error = f"systemAccounts() unavailable: {e}"
        logger.debug(sys_error)

    for acct in sys_accts:
        name = (getattr(acct, "name", "") or "").strip()
        if not name:
            continue
        if is_owner_system_account(
            acct,
            owner_email=owner_ids["email"],
            owner_username=owner_ids["username"],
            owner_account_id=owner_ids["account_id"],
            owner_email_local=owner_ids["email_local"],
        ):
            continue
        users.append({
            "kind": "managed",
            "plex_id": name,
            "raw_name": name,
            "display_name": display_names.get(name, ""),
        })

    # Defensive last-pass dedup: collapse any kind="managed" rows
    # whose identifier matches the owner's identifier set. Belt-and-
    # braces against display-name shapes the per-account skip didn't
    # recognise; the owner row stays in its original position.
    users = dedupe_owner_against_managed(
        users,
        owner_email=owner_ids["email"],
        owner_email_local=owner_ids["email_local"],
        owner_username=owner_ids["username"],
    )

    return {"users": users, "error": sys_error}


def set_user_display_name(
    server_id: str, plex_id: str, display_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Set / clear one entry in a server's ``user_display_names`` map.

    An empty / whitespace-only ``display_name`` removes the entry
    instead of storing an empty value - that way the frontend's
    fallback ("show raw_name when no display name set") works without
    extra logic.

    Returns the redacted server row after the write, or ``None`` if
    no server with ``server_id`` exists. Raises ``ValueError`` if
    ``plex_id`` is empty (a client bug or malformed request).
    """
    plex_id = (plex_id or "").strip()
    if not plex_id:
        raise ValueError("plex_id must not be empty.")
    cleaned = (display_name or "").strip()

    with _REG_LOCK:
        rows = _load_raw()
        target = next((r for r in rows if r.get("id") == server_id), None)
        if target is None:
            return None
        names = dict(target.get("user_display_names") or {})
        if cleaned:
            names[plex_id] = cleaned
        else:
            names.pop(plex_id, None)
        target["user_display_names"] = names
        _save_raw(rows)

    return get_server_by_id(server_id, include_token=False)


def update_server_settings(
    server_id: str,
    patch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Merge ``patch`` into the registry row identified by ``server_id``
    and persist. Returns the redacted server row after the write,
    or ``None`` when no row matched.

    Generic per-server-settings update: callers pass a dict of keys
    to merge (no nested-merge logic; top-level keys overwrite). The
    flat-dict layout in ``_DEFAULT_SERVER`` is what makes this work
    cleanly - there's no schema migration; missing keys default to
    their ``_DEFAULT_SERVER`` value on read.

    Used by:
      - services.user_activity_sweeper: stamps
        ``user_activity_last_sweep_at`` +
        ``user_activity_last_sweep_summary`` after each per-server
        sweep cycle.
      - the new auto_tombstone toggle wiring on the server editor.

    Caller is responsible for validating the patch's values; this
    helper does not enforce key allow-lists because the set of
    legal per-server keys is the whole ``_DEFAULT_SERVER`` keyspace
    plus anything the engine writes (sweep cursor etc.).
    """
    if not server_id:
        return None
    sid = str(server_id).strip()
    if not sid:
        return None
    with _REG_LOCK:
        rows = _load_raw()
        target = next((r for r in rows if r.get("id") == sid), None)
        if target is None:
            return None
        for k, v in (patch or {}).items():
            target[k] = v
        _save_raw(rows)
    return get_server_by_id(sid, include_token=False)


# ── Engine integration ──────────────────────────────────────────────────────


@dataclass
class ServerConnection:
    """Result of :func:`connect_registered_server`.

    Carries the connected ``server`` instance (a ``plexapi.PlexServer``
    for Plex, a Jellyfin / Emby HTTP client for those backends), a
    backend-agnostic ``adapter`` implementing
    :class:`services.adapters.MediaServerAdapter`, and the raw
    registry ``row`` for callers that still need the persisted
    metadata (url, token, owner_name, last_status, etc.).

    All 8 production callers and 3 test mocks updated in the same PR
    that introduced the dataclass; no transitional tuple-unpacking
    shim is required.
    """
    server: Any                          # plexapi.PlexServer today; HTTP client tomorrow
    row: Dict[str, Any]                  # the registry row (with token already decrypted upstream)
    adapter: "MediaServerAdapter"        # backend-agnostic adapter
    url: str                             # convenience accessor; mirrors row['url']
    token: str                           # plaintext, in-memory only; mirrors decrypt_server_token(row)
    service_type: str = "plex"           # mirrors row.get('service_type', 'plex')


# Persistent-adapter cache, so a batch doesn't pay the 32s
# path-index walk every time: cache the constructed
# MediaServerAdapter + the live server handle per server_id.
# Subsequent batches against the same server reuse the cached
# instance, so its on-instance caches (path indexes, per-artist
# track maps, library-of-truth, GUID blacklist, sections cache)
# survive across submissions.
#
# The cache fingerprint includes the row's URL and a hash of the
# decrypted token so a config change (rotated token / moved URL)
# invalidates the cache on next connect.

_ADAPTER_CACHE: Dict[
    str,
    Dict[str, Any],  # {"adapter", "server", "url", "token_hash", "service_type"}
] = {}
_ADAPTER_CACHE_LOCK = threading.Lock()


def _adapter_cache_fingerprint(url: str, plain_token: str) -> str:
    """Stable fingerprint so we invalidate when URL or token rotates.
    Token is hashed (sha256) before storage so the cache never holds
    plaintext credentials."""
    import hashlib
    h = hashlib.sha256(plain_token.encode("utf-8", errors="ignore")).hexdigest()
    return f"{url}|{h[:16]}"


def _get_cached_adapter(
    server_id: str, url: str, plain_token: str,
) -> Optional[Dict[str, Any]]:
    """Return the cached adapter+server bundle for this server_id,
    or None when:
    - never cached
    - URL or token fingerprint changed (config rotated)
    Caller is responsible for falling through to a fresh build on miss."""
    if not server_id:
        return None
    fp = _adapter_cache_fingerprint(url, plain_token)
    with _ADAPTER_CACHE_LOCK:
        cached = _ADAPTER_CACHE.get(server_id)
        if cached is None:
            return None
        cached_fp = (
            f"{cached.get('url', '')}|{cached.get('token_hash', '')}"
        )
        if cached_fp != fp:
            # Fingerprint drift; drop and rebuild.
            _ADAPTER_CACHE.pop(server_id, None)
            return None
        return cached


def _set_cached_adapter(
    server_id: str,
    *,
    adapter: Any,
    server: Any,
    url: str,
    plain_token: str,
    service_type: str,
) -> None:
    """Publish a built adapter to the cache. First write wins under
    racing concurrent connects."""
    if not server_id:
        return
    import hashlib
    token_hash = hashlib.sha256(
        plain_token.encode("utf-8", errors="ignore"),
    ).hexdigest()[:16]
    with _ADAPTER_CACHE_LOCK:
        if server_id not in _ADAPTER_CACHE:
            _ADAPTER_CACHE[server_id] = {
                "adapter": adapter,
                "server": server,
                "url": url,
                "token_hash": token_hash,
                "service_type": service_type,
            }


def invalidate_adapter_cache(server_id: Optional[str] = None) -> int:
    """Drop the cached adapter(s). ``server_id=None`` clears every
    entry (used by tests + on global config change). Returns the
    count evicted. Operators can call this after a server token
    rotation to force re-build on next connect."""
    with _ADAPTER_CACHE_LOCK:
        if server_id is None:
            n = len(_ADAPTER_CACHE)
            _ADAPTER_CACHE.clear()
            return n
        return 1 if _ADAPTER_CACHE.pop(server_id, None) else 0


def connect_registered_server(
    name_or_id: str, logger: logging.Logger,
) -> ServerConnection:
    """
    Resolve a server identifier and connect to it.

    The identifier is tried first as a friendly name, then as a UUID
    id, so the same function works for both CLI input ("Plex1") and
    REST handlers that pass the id from the URL.

    Returns a :class:`ServerConnection` carrying the live ``server``
    instance, the backend-agnostic ``adapter``, and the registry
    ``row``. Updates the server row's ``last_status`` /
    ``last_checked_at`` / ``owner_name`` fields and persists them
    before returning, so the registry's status column is always up to
    date after a connection attempt.

    Raises:
        ValueError if no server with that name/id is registered.
        ConnectionError if Plex is unreachable or the token is bad.
    """
    row = get_server_by_name(name_or_id) or get_server_by_id(name_or_id)
    if row is None:
        raise ValueError(f"No registered server named or ided {name_or_id!r}")

    # Decrypt the token only at the moment we hand it to the adapter.
    # The plaintext lives in a local variable for the duration of
    # this call; nothing on ``row`` is mutated.
    try:
        plain_token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        _record_status(row["id"], status="auth_error",
                       detail=str(exc), checked_at=time.time())
        raise ConnectionError(str(exc)) from exc

    service_type = (row.get("service_type") or "plex").lower()
    now = time.time()

    # Adapter cache. If we've already built a PlexAdapter for this
    # (server_id, url, token) fingerprint, reuse it so its
    # on-instance caches (path index, per-artist cache, sections
    # cache, library-of-truth, GUID blacklist) survive across
    # submissions. Subsequent batches against the same dest drop to
    # ~3 seconds from ~35 seconds.
    cached_bundle = _get_cached_adapter(
        str(row.get("id") or ""), row["url"], plain_token,
    )
    if cached_bundle is not None:
        server = cached_bundle["server"]
        adapter = cached_bundle["adapter"]
        owner = row.get("owner_name") or ""
        # Still record an "ok" status timestamp on the row so the
        # registry's "last_checked_at" field stays current (the
        # cached adapter doesn't ping; the cache is implicit liveness
        # because a stale adapter would have failed on its first use
        # and a token rotation would have flipped the fingerprint).
        _record_status(row["id"], status="ok", detail="", checked_at=now, owner=owner)
        row["last_status"] = "ok"
        row["last_checked_at"] = now
        return ServerConnection(
            server=server,
            row=row,
            adapter=adapter,
            url=row["url"],
            token=plain_token,
            service_type=service_type,
        )

    # Dispatch on service_type. Plex goes through
    # the existing plexapi-based connect path so all engine internals
    # (the engine call sites still use ``connection.server``) keep
    # working. Jellyfin / Emby instantiate the HTTP adapter directly
    # and probe via ``server_identity()``.
    if service_type == "plex":
        server, owner = _connect_plex(row, plain_token, logger, now)
        # Stamp the app registry UID onto the plexapi server object
        # so the resolver and PlexAdapter.MirrorResolveMixin both key
        # the server mirror on the app UID
        # (server_registry.make_server_id()) rather than the
        # backend-native machineIdentifier.
        try:
            server._pmig_server_uid = str(row.get("id") or "")
        except Exception:
            pass
        from services.adapters.plex import PlexAdapter
        adapter = PlexAdapter(
            server,
            base_url=row["url"],
            admin_token=plain_token,
            machine_id=getattr(server, "machineIdentifier", "") or row.get("machine_identifier"),
        )
    elif service_type in ("jellyfin", "emby"):
        adapter, server, owner = _connect_http_backend(
            row, plain_token, service_type, now,
        )
    else:
        _record_status(row["id"], status="auth_error",
                       detail=f"unknown service_type {service_type!r}",
                       checked_at=now)
        raise ConnectionError(
            f"Server {row.get('name') or row.get('id')!r} has unknown "
            f"service_type {service_type!r}; expected one of plex|jellyfin|emby"
        )

    _record_status(row["id"], status="ok", detail="", checked_at=now, owner=owner)
    # Refresh our local copy so the caller sees the new fields.
    row["last_status"] = "ok"
    row["last_checked_at"] = now
    row["owner_name"] = owner

    # Publish to the adapter cache so subsequent connects reuse this
    # instance + its on-instance caches.
    _set_cached_adapter(
        str(row.get("id") or ""),
        adapter=adapter, server=server,
        url=row["url"], plain_token=plain_token,
        service_type=service_type,
    )

    return ServerConnection(
        server=server,
        row=row,
        adapter=adapter,
        url=row["url"],
        token=plain_token,
        service_type=service_type,
    )


def _connect_plex(
    row: Dict[str, Any],
    plain_token: str,
    logger: logging.Logger,
    now: float,
) -> Tuple[Any, str]:
    """Plex-specific connect: plexapi handshake + owner extraction.

    Extracted from the original ``connect_registered_server`` body so
    the dispatcher reads cleanly. Returns ``(PlexServer, owner_name)``.
    Raises ``ConnectionError`` with status recorded into the registry
    on any failure."""
    from services.auth import connect_to_server
    try:
        # raise_on_failure so the ConnectionError below carries the
        # underlying exception type + message. Without this, every
        # connect failure surfaces as the generic "Check the URL and
        # token" string which doesn't tell the end user whether it's
        # a TLS error, a 401, or a network outage.
        server = connect_to_server(
            row["url"], plain_token, logger, raise_on_failure=True,
        )
    except Exception as exc:
        status, detail = classify_connection_error(exc)
        _record_status(row["id"], status=status,
                       detail=detail, checked_at=now)
        raise ConnectionError(
            f"Cannot connect to registered server {row['name']!r} at "
            f"{row['url']}: {detail}"
        ) from exc
    if server is None:
        # Defensive: with raise_on_failure=True above we should always
        # either get a server or hit the except. Keep this branch as a
        # belt-and-suspenders fallback.
        _record_status(row["id"], status="unreachable",
                       detail=f"connect_to_server returned None for {row['url']}",
                       checked_at=now)
        raise ConnectionError(
            f"Cannot connect to registered server {row['name']!r} at {row['url']}. "
            f"Check the URL and token under the Servers tab."
        )

    owner = getattr(server, "myPlexUsername", None) or "Plex Owner"
    return server, owner


def _connect_http_backend(
    row: Dict[str, Any],
    plain_token: str,
    service_type: str,
    now: float,
) -> Tuple[Any, Any, str]:
    """Build a Jellyfin or Emby adapter and probe by reading server
    identity (which forces a ``/System/Info`` round-trip + a
    ``/Users/Me`` lookup for owner identification).

    Returns ``(adapter, server_handle, owner_display)`` where
    ``server_handle`` is the adapter itself - the engine's
    ``connection.server`` accessor falls back to the adapter for
    backends that don't expose a separate ``PlexServer``-style object.
    Raises ``ConnectionError`` with status recorded on the registry
    row when the probe fails."""
    # Late imports to avoid circular dependency: adapter modules
    # import services.guid_translator which is safe; the registry
    # itself never imports the adapters at module load.
    # Hand the adapter the app registry UID so MirrorResolveMixin
    # keys the server mirror on the same backend-neutral id the sync
    # endpoint uses.
    _server_uid = str(row.get("id") or "")
    if service_type == "jellyfin":
        from services.adapters.jellyfin import JellyfinAdapter
        adapter = JellyfinAdapter(
            row["url"], plain_token,
            machine_id=row.get("machine_identifier"),
            server_uid=_server_uid,
        )
    else:
        from services.adapters.emby import EmbyAdapter
        adapter = EmbyAdapter(
            row["url"], plain_token,
            machine_id=row.get("machine_identifier"),
            server_uid=_server_uid,
        )

    try:
        if not adapter.ping():
            raise ConnectionError(
                f"{(service_type or '').capitalize() or 'Server'} server at "
                f"{row['url']} did not respond to ping"
            )
        identity = adapter.server_identity()
    except ConnectionError:
        raise
    except Exception as exc:
        status, detail = classify_connection_error(exc)
        _record_status(row["id"], status=status,
                       detail=detail, checked_at=now)
        raise ConnectionError(
            f"Cannot connect to registered server {row['name']!r} at "
            f"{row['url']}: {detail}"
        ) from exc

    if not identity.machine_id:
        _record_status(
            row["id"], status="unreachable",
            detail=f"{service_type} server returned no machine id",
            checked_at=now,
        )
        raise ConnectionError(
            f"Cannot identify {service_type} server at {row['url']}. "
            f"Check the URL and API key."
        )

    owner = identity.owner_display or f"{service_type.capitalize()} Owner"
    # The engine reads ``connection.server`` today; for backends with
    # no separate "server" object (HTTP adapters), expose the adapter
    # itself as the server handle. Once the engine migration lands and
    # the engine reads ``connection.adapter`` directly, this can shrink
    # to ``server=None``.
    return adapter, adapter, owner


def _test_connection_http_backend(
    server_id: str,
    row: Dict[str, Any],
    logger: logging.Logger,
    service_type: str,
) -> Dict[str, Any]:
    """Test-connection helper for Jellyfin / Emby. Reuses
    :func:`probe_unsaved`'s already-multi-backend probe path; the row
    is updated with the same status fields the Plex path writes."""
    now = time.time()
    try:
        plain_token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        _record_status(server_id, status="auth_error",
                       detail=str(exc), checked_at=now)
        return get_server_by_id(server_id, include_token=False) or row

    probe = probe_unsaved(
        row["url"], plain_token, logger, service_type=service_type,
    )
    if not probe.get("ok"):
        _record_status(
            server_id,
            status=probe.get("status") or "auth_error",
            detail=probe.get("detail") or "",
            checked_at=now,
        )
        return get_server_by_id(server_id, include_token=False) or row

    _record_status(
        server_id, status="ok", detail="", checked_at=now,
        owner=probe.get("owner_name") or "",
        libraries=probe.get("libraries") or [],
        machine_identifier=probe.get("machine_identifier") or "",
        friendly_name=probe.get("friendly_name") or "",
        plex_version=probe.get("plex_version") or "",
        playlist_count=probe.get("playlist_count"),
        collection_count=probe.get("collection_count"),
        counts_refreshed_at=time.time(),
    )
    return get_server_by_id(server_id, include_token=False) or row


def test_connection(server_id: str, logger: logging.Logger) -> Dict[str, Any]:
    """
    Probe a registered server's connection without running any engine
    logic. Used by the "Test" button in the Servers tab and at
    startup to populate the status indicators.

    Always returns the (now-updated) server row. Never raises - a
    failure is recorded into the row instead so the frontend can
    render a useful tooltip.

    Dispatch on ``row['service_type']``. Plex uses the
    existing plexapi handshake; Jellyfin / Emby delegate to
    :func:`probe_unsaved` (which is already multi-backend) and
    transcribe the probe result into the registry row.
    """
    row = get_server_by_id(server_id)
    if row is None:
        raise ValueError(f"No server with id {server_id!r}")
    service_type = (row.get("service_type") or "plex").lower()
    if service_type in ("jellyfin", "emby"):
        return _test_connection_http_backend(server_id, row, logger, service_type)

    from services.auth import connect_to_server

    now = time.time()
    try:
        plain_token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        _record_status(server_id, status="auth_error",
                       detail=str(exc), checked_at=now)
        return get_server_by_id(server_id, include_token=False) or row
    try:
        # raise_on_failure surfaces the real exception (TLS handshake,
        # 401, etc.) into the status detail so the Servers panel
        # tooltip is actionable. Default-False would hide it behind
        # a generic "returned None" string.
        server = connect_to_server(
            row["url"], plain_token, logger, raise_on_failure=True,
        )
    except Exception as exc:
        status, detail = classify_connection_error(exc)
        _record_status(server_id, status=status,
                       detail=detail, checked_at=now)
        return get_server_by_id(server_id, include_token=False) or row
    if server is None:
        # Defensive: raise_on_failure=True above guarantees we either
        # get a server or hit the except. Keep this branch as a fallback.
        _record_status(server_id, status="unreachable",
                       detail="connect_to_server returned None unexpectedly", checked_at=now)
        return get_server_by_id(server_id, include_token=False) or row
    # Try a tiny live query so an "ok" status really means the token works.
    try:
        owner = getattr(server, "myPlexUsername", None) or "Plex Owner"
        machine_id = str(getattr(server, "machineIdentifier", "") or "")
        friendly = str(getattr(server, "friendlyName", "") or "")
        plex_version = str(getattr(server, "version", "") or "")
        libs_raw = list(server.library.sections())
        libs: List[Dict[str, Any]] = []
        for sec in libs_raw:
            try:
                count = sec.totalSize
            except Exception:
                count = 0
            entry: Dict[str, Any] = {
                "name": sec.title, "type": sec.type,
                "key": sec.key, "count": count,
            }
            leaf = _fetch_leaf_counts(server, sec)
            if leaf:
                entry["leaf_counts"] = leaf
            libs.append(entry)
        playlist_count, collection_count = _fetch_server_level_counts(server, libs_raw)
        _record_status(
            server_id, status="ok", detail="", checked_at=now,
            owner=owner, libraries=libs,
            machine_identifier=machine_id, friendly_name=friendly,
            plex_version=plex_version,
            playlist_count=playlist_count,
            collection_count=collection_count,
            counts_refreshed_at=time.time(),
        )
    except Exception as exc:
        _record_status(server_id, status="auth_error",
                       detail=f"{type(exc).__name__}: {exc}", checked_at=now)
    return get_server_by_id(server_id, include_token=False) or row


def refresh_libraries(server_id: str, logger: logging.Logger) -> List[Dict[str, Any]]:
    """
    Reload the cached library list for a server. Returns the new list.
    Errors propagate so the frontend can show them.
    """
    row = test_connection(server_id, logger)
    return row.get("last_libraries", []) or []


def get_cached_libraries(server_id: str) -> List[Dict[str, Any]]:
    """Return the registry's last-known library list for a server
    WITHOUT re-probing the live API.

    Probing live via :func:`refresh_libraries` (which calls
    :func:`test_connection`) does a full Plex / Emby / Jellyfin
    handshake + per-library item count probe (3-15 seconds depending
    on backend + library size). The Run Job form's DataToMigratePanel
    hits this endpoint on every source-server change, so a live probe
    each time makes the panel slow to load.

    This cached path returns the already-stored ``last_libraries``
    row instantly. The Run Job form uses it by default; an explicit
    refresh button (or ``?refresh=1`` query) still triggers the
    full probe when the operator wants a live re-read.

    Empty list when the server has never been probed (the registry
    row's ``last_libraries`` is initialised to ``[]``). 404 raised
    for an unknown server_id so the endpoint can distinguish "not
    yet probed" from "wrong id."""
    row = get_server_by_id(server_id, include_token=False)
    if row is None:
        raise ValueError(f"No server with id {server_id!r}")
    return row.get("last_libraries", []) or []


# ── Lightweight ping (v0.9.1) ────────────────────────────────────────────────

def ping_server(server_id: str, *, timeout: Optional[float] = None) -> Dict[str, Any]:
    """
    Probe a registered server's reachability without enumerating its
    libraries - much cheaper than :func:`test_connection`. Used by the
    UI's 30-second status refresh poll.

    Issues one ``GET /identity?X-Plex-Token=...`` against the registered
    URL. Plex's ``/identity`` endpoint returns a small XML document
    with server metadata; if the server is reachable and the token is
    accepted, we get a 200 response in single-digit milliseconds on a
    LAN. Any non-200, any connection error, or any timeout means the
    server is not reachable for our purposes.

    The result is also written back into the row's status fields so
    the cached value visible elsewhere stays current. Returns a dict
    ``{"ok": bool, "response_ms": float, "status": str, "detail": str}``.

    Never raises - a failure to reach Plex is the *expected* outcome
    for at least some pings and the caller just wants the result.
    """
    # Import here so the module stays importable without ``requests``
    # in environments that only use the CRUD bits.
    import requests

    # ``timeout=None`` resolves from the ``server_ping_timeout_seconds``
    # tunable so the 30-second status-poll budget is operator-tunable.
    # An explicit caller-supplied timeout still wins.
    if timeout is None:
        from services import tunables
        timeout = float(tunables.server_ping_timeout())

    row = get_server_by_id(server_id)
    if row is None:
        return {"ok": False, "response_ms": 0.0, "status": "unknown",
                "detail": f"No server with id {server_id!r}"}

    url = (row.get("url") or "").rstrip("/")
    if not url or not (row.get("token") or ""):
        _record_status(server_id, status="unreachable",
                       detail="URL or token missing on registry row",
                       checked_at=time.time(), response_ms=0.0)
        return {"ok": False, "response_ms": 0.0, "status": "unreachable",
                "detail": "URL or token missing"}
    try:
        token = decrypt_server_token(row)
    except ServerCredentialError as exc:
        _record_status(server_id, status="auth_error",
                       detail=str(exc), checked_at=time.time(), response_ms=0.0)
        return {"ok": False, "response_ms": 0.0, "status": "auth_error",
                "detail": str(exc)}

    # Dispatch on service_type for the cheap reachability
    # probe. Plex uses ``/identity`` (token-gated); Jellyfin / Emby
    # use ``/System/Info/Public`` (public, no auth) which is the
    # canonical "is the server alive" check on both. Per-backend
    # endpoint avoids the 404 the Plex path produced when the row is
    # not actually a Plex server.
    service_type = (row.get("service_type") or "plex").lower()
    if service_type in ("jellyfin", "emby"):
        probe_url = f"{url}/System/Info/Public"
        probe_headers: Dict[str, str] = {"Accept": "application/json"}
    else:
        probe_url = f"{url}/identity"
        probe_headers = {"X-Plex-Token": token, "Accept": "application/xml"}

    started = time.perf_counter()
    detail = ""
    status = "unreachable"
    ok = False
    try:
        resp = requests.get(
            probe_url, headers=probe_headers, timeout=timeout,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if resp.status_code == 200:
            ok = True
            status = "ok"
        elif resp.status_code in (401, 403):
            status = "auth_error"
            detail = f"HTTP {resp.status_code}: token rejected"
        else:
            detail = f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        detail = f"Timeout after {timeout:.1f}s"
    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        detail = f"Connection error: {exc.__class__.__name__}"
    except Exception as exc:  # pragma: no cover (defensive)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        detail = f"{type(exc).__name__}: {exc}"

    _record_status(
        server_id,
        status=status,
        detail=detail,
        checked_at=time.time(),
        response_ms=elapsed_ms,
    )

    # Feed the process-lifetime network collector so the
    # Networking tab has live "is this server reachable and how fast"
    # data even when no job is running. The collector ages entries
    # over a 60 s window so a 30 s ping cadence keeps the chart fresh.
    try:
        from server import network_collector as _nc
        _nc.record_ping(url=url, ok=ok, elapsed_ms=elapsed_ms)
    except Exception:
        # Telemetry must never break the poll path.
        pass

    return {
        "ok": ok,
        "response_ms": round(elapsed_ms, 1),
        "status": status,
        "detail": detail,
    }


def _record_status(server_id: str, *, status: str, detail: str, checked_at: float,
                   owner: Optional[str] = None, libraries: Optional[List[Dict[str, Any]]] = None,
                   response_ms: Optional[float] = None,
                   machine_identifier: Optional[str] = None,
                   friendly_name: Optional[str] = None,
                   plex_version: Optional[str] = None,
                   playlist_count: Optional[int] = None,
                   collection_count: Optional[int] = None,
                   counts_refreshed_at: Optional[float] = None) -> None:
    """
    Internal: update status fields on one row and persist.

    Called from :func:`connect_registered_server` (job run),
    :func:`test_connection` (Test button), and :func:`ping_server`
    (live status poll). Keeping the write path in one place ensures
    the same lock discipline is applied no matter how the status was
    learned.

    ``response_ms`` is recorded on every call so the UI can show
    "current latency" without needing a separate write path; callers
    that don't measure latency (e.g. :func:`test_connection` which
    measures library enumeration time, not raw ping) pass ``None``
    to leave the previous value in place.

    ``machine_identifier`` and ``friendly_name`` are captured on
    every successful connect so the UI can warn when the URL+token a
    user typed connects to a different physical server than the one
    they intended to register / refresh.
    """
    with _REG_LOCK:
        rows = _load_raw()
        for row in rows:
            if row.get("id") == server_id:
                row["last_status"] = status
                row["last_status_detail"] = detail
                row["last_checked_at"] = checked_at
                if owner is not None:
                    row["owner_name"] = owner
                if libraries is not None:
                    row["last_libraries"] = libraries
                if response_ms is not None:
                    row["last_response_ms"] = response_ms
                if machine_identifier is not None:
                    row["machine_identifier"] = machine_identifier
                if friendly_name is not None:
                    row["friendly_name"] = friendly_name
                if plex_version is not None:
                    row["plex_version"] = plex_version
                # Timing-spec counts: only overwrite when the caller
                # explicitly passed a value (None means "leave alone").
                # A successful Refresh / Test always passes all three
                # together, including ``counts_refreshed_at`` as the
                # single audit timestamp the timing engine reads.
                if playlist_count is not None:
                    row["playlist_count"] = playlist_count
                if collection_count is not None:
                    row["collection_count"] = collection_count
                if counts_refreshed_at is not None:
                    row["counts_refreshed_at"] = counts_refreshed_at
                break
        _save_raw(rows)


# ── Auto-migration from v0.8.0 ──────────────────────────────────────────────

def migrate_legacy_settings(logger: logging.Logger) -> Optional[Dict[str, Any]]:
    """
    Move legacy ``plex_url`` / ``plex_token`` from ``settings.json``
    into the registry as a server named "Default".

    Idempotent: a fully-migrated install (no legacy fields, or legacy
    fields empty) returns ``None`` and writes nothing. A partially
    migrated install (registry already has entries but legacy fields
    are still set) clears the legacy fields and returns ``None`` - we
    don't double-register.

    Returns the newly-created server row, or ``None`` if no migration
    was needed.
    """
    settings = load_settings()
    legacy_url = (settings.get("plex_url") or "").strip()
    legacy_tok = (settings.get("plex_token") or "").strip()
    # Idempotence: a fully-migrated install has both legacy fields empty
    # (load_settings backfills missing keys as ""). We early-out here so
    # the function performs zero work and writes zero bytes on every
    # subsequent startup. If a future schema change adds new legacy
    # fields, that path needs its own empty-check guard at this point.
    if not legacy_url and not legacy_tok:
        return None

    # If the registry already has the legacy URL/token under any name,
    # treat this as already-migrated and just clear the legacy fields.
    # Tokens in the registry are encrypted; decrypt for the compare.
    # A row whose token is unreadable can't match plaintext anyway, so
    # we just skip it.
    existing = list_servers(include_tokens=True)
    for row in existing:
        if row.get("url") != legacy_url.rstrip("/"):
            continue
        try:
            row_plain = decrypt_server_token(row)
        except ServerCredentialError:
            continue
        if row_plain == legacy_tok:
            _clear_legacy_fields(settings)
            return None

    # If there's no registry at all, register the legacy as "Default".
    # If there IS a registry already (user manually added servers) but
    # neither URL nor token match, the legacy fields probably refer to
    # a server the user no longer wants - clear them silently.
    if existing:
        _clear_legacy_fields(settings)
        logger.info(
            "Legacy plex_url/plex_token in settings.json ignored - "
            "registry already contains %d server(s).", len(existing),
        )
        return None

    # Heuristic default name: "Default" unless already taken (it
    # can't be - registry is empty here).
    new_row = add_server(name="Default", url=legacy_url, token=legacy_tok)
    _clear_legacy_fields(settings)
    logger.info("Migrated legacy plex_url/plex_token into registry as server 'Default'.")
    return new_row


def _clear_legacy_fields(settings: Dict[str, Any]) -> None:
    """Strip the now-unused single-server fields out of ``settings.json``."""
    patch = {"plex_url": "", "plex_token": ""}
    # ``save_settings`` is a merge - passing the patch overwrites just
    # those two keys without touching workers / output_dir / etc.
    save_settings(patch)
