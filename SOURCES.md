# PlexMigrate - Documentation Sources

This file records every external resource, API documentation page, library
reference, and concept source that informed decisions made in each version.

Append only - never truncate or overwrite previous entries.

---

## Sources - v0.2.0 - 2026-05-09

### What These Sources Cover

Covers the initial build (v0.1.0) and the v0.2.0 additive merge redesign. Libraries and APIs documented here: python-plexapi, Plex's direct HTTP endpoints, Python's threading and concurrency tools, and the MusicBrainz identifier system for cross-server music matching.

| # | Source Name | URL | What It Covers | How It Was Used |
|---|---|---|---|---|
| 1 | python-plexapi documentation | https://python-plexapi.readthedocs.io/en/latest/ | Full API reference for PlexServer, LibrarySection, Playlist, Collection, and media objects | Used to identify correct method calls: `getByGuid()`, `markWatched()`, `Playlist.create()`, `Playlist.addItems()`, `Collection.create()`, `Collection.addItems()`, `section.search()`, `section.collections()`, `server.playlists()` |
| 2 | python-plexapi GitHub repository | https://github.com/pkkid/python-plexapi | Source code for PlexServer, Playlist, and Collection classes | Consulted to verify that `addItems()` exists on both Playlist and Collection in plexapi >= 4.0, and to understand what `markWatched()` does internally (it calls `/:/scrobble`) |
| 3 | Python `concurrent.futures` documentation | https://docs.python.org/3/library/concurrent.futures.html | How to create and manage thread pools; `ThreadPoolExecutor`, `as_completed()`, `Future.exception()` | Used to design the multi-stage parallel pipeline for library snapshot and the concurrent watch history import |
| 4 | Python `queue.Queue` documentation | https://docs.python.org/3/library/queue.html | Thread-safe queue for producer-consumer patterns; `put()`, `get()`, `Empty` exception | Used to pass progress signals from snapshot worker threads to the tqdm progress-watcher thread |
| 5 | Python `threading` documentation | https://docs.python.org/3/library/threading.html | `threading.Lock` for protecting shared mutable state; `threading.Thread` with `daemon=True` | Used for `_log_lock` (protects `_lib_successes`, `_lib_failures`, `_failure_categories`) and for the progress-watcher daemon thread in `run_snapshot()` |
| 6 | Python `logging` documentation | https://docs.python.org/3/library/logging.html | How to configure handlers, formatters, and log levels; `FileHandler`, `StreamHandler` | Used to build the shared logger in `setup_logging()` with both a file handler (always DEBUG) and a console handler (INFO or DEBUG based on --verbose) |
| 7 | Python `argparse` documentation | https://docs.python.org/3/library/argparse.html | How to define flags, handle `nargs`, `action="store_true"`, `dest=`, and mutually exclusive arguments | Used to build the CLI in `build_parser()` |
| 8 | Python `pathlib.Path` documentation | https://docs.python.org/3/library/pathlib.html | Cross-platform filesystem path handling; `mkdir(parents=True, exist_ok=True)`, `Path.exists()` | Used throughout for creating directories and checking file existence without OS-specific path separators |
| 9 | MusicBrainz Identifier documentation | https://musicbrainz.org/doc/MusicBrainz_Identifier | What MusicBrainz IDs are, how they are assigned, and why they are globally unique | Used to explain why `plex://` and `mb://` GUIDs are portable across servers while `local://` GUIDs are not |
| 10 | tqdm documentation | https://tqdm.github.io/ | Progress bar API; `tqdm(total=N)`, `pbar.update()`, `leave=False` | Used for live progress bars during snapshot and import operations |
| 11 | rich library documentation | https://rich.readthedocs.io/en/stable/ | `Console`, `Table`, `Prompt.ask()` for formatted terminal output | Used for the library discovery table, mode prompt, and colour-coded status messages |
| 12 | requests library documentation | https://requests.readthedocs.io/en/latest/ | `requests.get()`, `requests.put()`, `params=`, `timeout=` | Used for direct Plex HTTP API calls that python-plexapi does not expose as methods: `/:/scrobble`, `/:/progress`, `/:/rate` |
| 13 | Plex `/:/scrobble` endpoint | [no specific public documentation URL] | Marks an item as watched, increments viewCount by 1, sets isWatched=True | Used in `_scrobble()` to add views one at a time as part of the delta merge |
| 14 | Plex `/:/progress` endpoint | [no specific public documentation URL] | Updates the resume position (viewOffset) of an item without changing viewCount when `state=stopped` | Used in `_set_resume_position()` to restore where the user paused |
| 15 | Plex `/:/rate` endpoint | [no specific public documentation URL] | Sets a user star rating (0–10) on a media item | Used in `_rate_item()` since python-plexapi does not expose a first-class rating method |
| 16 | Python `xml.etree.ElementTree` documentation | https://docs.python.org/3/library/xml.etree.elementtree.html | Parsing XML files; `ET.parse()`, `root.get()` | Used to read the `PlexOnlineToken` attribute from Plex's `Preferences.xml` file |
| 17 | Python `set` data structure documentation | https://docs.python.org/3/tutorial/datastructures.html#sets | O(1) membership testing with `in`; set construction from an iterable | Used in playlist and collection merge logic to build `Set[int]` of existing ratingKeys for fast duplicate detection |

### Concept Explanations

**python-plexapi**: A community Python library (not from Plex Inc.) that wraps Plex's HTTP API in Python objects. Instead of writing raw HTTP requests, you write `server.playlists()` or `item.markWatched()`. PlexMigrate talks to Plex through it whenever possible and falls back to raw HTTP only for endpoints the library doesn't expose (`/:/rate`, `/:/progress`).

**MusicBrainz GUIDs (the `mb://` prefix)**: MusicBrainz is a free, open music encyclopedia where every song, album, and artist has a globally unique ID. When Plex matches your music library to MusicBrainz, it stores that ID alongside each track. The ID is the same everywhere in the world for that track, so we can use it to find the same track on a different Plex server. Tracks that haven't been matched to MusicBrainz get a `local://` GUID instead. Those IDs are only meaningful on the one server, which makes cross-server matching hard. The README's "Fix Match" advice exists for that reason.

**Plex GUIDs (the `plex://` prefix)**: Plex maintains its own metadata database (Plex Media Database, or PMDb) that assigns unique IDs to movies, TV shows, and music. Like MusicBrainz IDs for music, `plex://` GUIDs are stable across different Plex servers for the same item, so they're reliable for cross-server matching.

**ThreadPoolExecutor**: Python runs one piece of code at a time by default. A ThreadPoolExecutor creates a pool of worker threads that run tasks in parallel. It matters here because PlexMigrate spends most of its time waiting for Plex to respond to HTTP requests. While one thread waits, the others send their own requests. 8 threads might run 5–6× faster than serial execution even on a 4-core machine.

**Producer-consumer pattern with `queue.Queue`**: In the snapshot pipeline, producer threads (the ones doing library snapshot work) put completion signals onto a shared queue. A single consumer thread reads the queue and updates the progress bar. The queue is a safe handoff point: producers don't need to know about the progress bar, the consumer doesn't need to know about snapshot logic, and `queue.Queue` handles thread safety internally.

**`threading.Lock`**: When multiple threads update the same Python object at once, one might overwrite another's write. A `Lock` prevents that. Only one thread holds the lock at a time; any other thread that tries to acquire it waits until the current holder releases. In PlexMigrate, `_log_lock` protects `_lib_successes`, `_lib_failures`, and `_failure_categories` from concurrent writes.

**Plex `ratingKey`**: Every item in a Plex library has an integer ID called `ratingKey`, assigned locally by that server's database. The same movie might be ratingKey `1234` on the old server and `5678` on the new one, so PlexMigrate can't use ratingKey for cross-server matching. We use GUIDs and file paths instead. Within a single server session ratingKey is stable and unique, so we do use it for duplicate detection when merging playlists and collections on the target.

**`viewOffset` (resume position)**: When you pause a video, Plex saves your position in milliseconds as `viewOffset`. Next time you open the item, Plex offers to resume from where you left off. PlexMigrate restores this value via the `/:/progress` endpoint, but only when the target server has `viewOffset == 0`. That guard prevents overwriting a newer resume position the user has already set.

**Set membership lookup in Python**: A Python `set` stores values in a hash table, so `value in my_set` is O(1) regardless of the set's size. `value in my_list` is O(n) because it scans every element. For playlist merge logic where hundreds of new items need checking against thousands of existing ones, the speed difference matters. PlexMigrate builds a `Set[int]` of existing `ratingKey` values once, then uses `item.ratingKey not in existing_keys` for each candidate.

---

## Sources - v0.3.0 - 2026-05-09

### What These Sources Cover

Covers the four gap fixes introduced in v0.3.0. Documented here: libtype-aware library iteration methods in python-plexapi, the Plex Home managed-user API, and the `playlist.smart` attribute for detecting smart playlists.

| # | Source Name | URL | What It Covers | How It Was Used |
|---|---|---|---|---|
| 18 | python-plexapi `MusicSection` documentation | https://python-plexapi.readthedocs.io/en/latest/modules/library.html#plexapi.library.MusicSection | `MusicSection.searchTracks()`, `MusicSection.searchAlbums()`, `MusicSection.searchArtists()` - methods for fetching music items at each level | Used to replace `section.search(unwatched=False)` (which is invalid for music) with `section.searchTracks()` in `snapshot_watch_history` and `snapshot_ratings` |
| 19 | python-plexapi `ShowSection` documentation | https://python-plexapi.readthedocs.io/en/latest/modules/library.html#plexapi.library.ShowSection | `ShowSection.searchEpisodes()` - fetches all episodes across all shows in the section | Used to replace `section.search()` (which returns Show objects) with `section.searchEpisodes()` in `snapshot_watch_history`, ensuring episode-level viewCount data is captured |
| 20 | python-plexapi `MyPlexAccount` documentation | https://python-plexapi.readthedocs.io/en/latest/modules/myplex.html#plexapi.myplex.MyPlexAccount | `server.myPlexAccount()`, `account.users()`, `user.get_token(machineIdentifier)` - the API for enumerating home users and obtaining per-user auth tokens | Used to implement `get_home_users()`: enumerates managed users, retrieves a server-scoped token for each, and creates a `PlexServer` connection authenticated as that user |
| 21 | python-plexapi `Playlist` source - `smart` attribute | https://github.com/pkkid/python-plexapi/blob/master/plexapi/playlist.py | `Playlist.smart` (bool) - True for smart (filter-based) playlists; `Playlist.content` (str) - the filter URL that defines the playlist's contents | Used in `serialize_playlist` to detect smart playlists, save the filter URL, and skip item iteration. Used in `import_playlists` to route smart playlists to the `smart_playlist_skipped` failure category |
| 22 | Plex Media Server - Plex Home documentation | https://support.plex.tv/articles/203948776-managed-users/ | How Plex Home managed users work: each user is a separate profile with independent watch history, ratings, and playlists; each has their own auth token | Used to understand the data isolation model: admin and each home user have separate viewCounts and userRatings even for the same media file. Informed the decision to snapshot and import per-user data independently |
| 23 | Python `enumerate()` documentation | https://docs.python.org/3/library/functions.html#enumerate | `enumerate(iterable)` yields `(index, item)` pairs - the standard Python way to get a loop counter alongside the item | Used in `serialize_playlist` to capture each item's position index (`for position, item in enumerate(playlist.items())`) for playlist order preservation |

### Concept Explanations

**`section.type` and libtype-aware method selection**: Every `LibrarySection` in python-plexapi has a `.type` attribute: `"artist"` for music, `"show"` for TV, `"movie"` for movies. Each section type exposes different search methods. `MusicSection` has `searchTracks()`, `searchAlbums()`, and `searchArtists()`. `ShowSection` has `searchEpisodes()` and `searchShows()`. `MovieSection` has `all()` for top-level items. The generic `section.search(unwatched=False)` filter only works on video libraries and crashes with "Unknown filter field" on music. PlexMigrate now branches on `section.type` to call the right method for each library kind.

**Why watch history must be at the leaf level**: In Plex's data model the items you actually watch are "leaves": Movies, Episodes, Tracks. Each leaf has its own `viewCount` (how many times played) and `viewOffset` (resume position). Parent containers (Shows, Seasons, Artists, Albums) also expose a `viewCount`, but it's an aggregate rolled up from their children. You can't scrobble it. PlexMigrate works at the leaf level because `/:/scrobble` acts on one leaf at a time. Fetching at the container level would give inaccurate aggregates and the wrong `ratingKey` for scrobble calls.

**Plex Home managed users**: Plex Home lets one server be shared by multiple people, each with their own managed user profile. Managed users are fully independent viewers. They each have their own watch history, view counts, and star ratings, even for the same media file. When the admin watches a movie it bumps the admin's viewCount. When a managed user watches it, it bumps that user's viewCount. The two never mix. To read or write a managed user's data you authenticate as that user. Admin credentials only see admin data. PlexMigrate handles this by calling `user.get_token(machineIdentifier)` and creating a separate `PlexServer(base_url, user_token)` connection per user.

**Smart playlists in Plex**: A smart playlist's contents come from a saved filter query rather than a fixed list. Example: "all movies in the Action genre rated above 8." Plex stores the filter as a URL string in `playlist.content`, which includes server-specific library section IDs. Those IDs are assigned locally by each Plex database and differ between servers, so the filter URL isn't portable. Recreating the smart playlist requires a human to open Plex on the target and re-enter the filter criteria. PlexMigrate saves the `content` URL in the backup for reference and logs it prominently so the user has what they need.

**Playlist item order**: Regular (non-smart) Plex playlists are ordered. The position of each item is significant, and Plex stores and displays items in insertion order. When PlexMigrate resolves playlist items via multi-threaded matching, thread scheduling can cause items to resolve in any order. Without explicit position tracking, the reconstructed playlist would land in unpredictable order. PlexMigrate saves a `"position"` index for each item at snapshot time (using `enumerate()` for the 0-based index), then sorts resolved items by that index before passing them to `Playlist.create()` or `addItems()` at import time.

---

## Sources - v0.4.0 - 2026-05-10

### What These Sources Cover

Sources for the Rich Live display redesign (replacing tqdm with Rich's Progress + Live panel), the RichHandler logging integration, the HTTP retry adapter, and the urllib3 Retry utility. Also covers the Rich Progress column types used to build the per-library progress rows.

| # | Source Name | URL | What It Covers | How It Was Used |
|---|---|---|---|---|
| 24 | rich.live documentation | https://rich.readthedocs.io/en/stable/live.html | `Live(renderable, console=, refresh_per_second=)` context manager - renders a renderable and re-renders it in place at a fixed rate; log output above the live area scrolls normally | Used to wrap the entire snapshot and import operation so the progress panel stays pinned at the bottom while log lines scroll above |
| 25 | rich.progress documentation | https://rich.readthedocs.io/en/stable/progress.html | `Progress`, `TaskID`, `add_task()`, `update()`, `SpinnerColumn`, `BarColumn`, `MofNCompleteColumn`, `TextColumn`, `TimeRemainingColumn`; thread-safety note (internal RLock) | Used to build the per-library progress bars and the overall bar; `update()` called from worker threads directly because of the internal RLock |
| 26 | rich.logging documentation | https://rich.readthedocs.io/en/stable/logging.html | `RichHandler(console=, show_time=, show_path=, markup=, rich_tracebacks=)` - a `logging.Handler` subclass that routes log records through a Rich Console; designed to coexist with `Live` when both use the same Console | Used to replace `StreamHandler(sys.stdout)` in `setup_logging()` so log lines no longer corrupt the Live panel |
| 27 | requests.adapters.HTTPAdapter documentation | https://requests.readthedocs.io/en/latest/api/#requests.adapters.HTTPAdapter | `HTTPAdapter(max_retries=, pool_connections=, pool_maxsize=)` - mounts a retry policy and connection pool on a `requests.Session` for a URL prefix | Used in `_make_retry_adapter()` to wrap the `Retry` object and mount it on both the direct-call session and plexapi's internal session |
| 28 | urllib3.util.retry.Retry documentation | https://urllib3.readthedocs.io/en/stable/reference | `Retry(total=, connect=, read=, backoff_factor=, status_forcelist=, allowed_methods=, raise_on_status=)` - controls how many times and under what conditions a failed HTTP request is retried | Used in `_make_retry_adapter()` with `total=2`, `backoff_factor=0.5`, `status_forcelist=[500,502,503,504]`, `allowed_methods=frozenset(["GET","PUT"])` |
| 29 | Python `threading.Semaphore` documentation | https://docs.python.org/3/library/threading.html#threading.Semaphore | `Semaphore(n)` - allows at most n threads to enter the guarded block simultaneously; threads beyond n block at `acquire()` until a holder calls `release()` | Used as `scrobble_sem = threading.Semaphore(SCROBBLE_WORKERS)` in `import_watch_history()` to cap concurrent Plex database writes while item resolution runs at full `MAX_WORKERS` concurrency |

### Concept Explanations

**`rich.live.Live` and how it keeps the panel at the bottom**: Open a `Live` context and Rich takes control of the terminal below the current cursor position. Content the panel produces is rendered there and re-rendered in place at `refresh_per_second` intervals. Log lines printed through a `RichHandler` that shares the same `Console` are injected above the live area and scroll up normally, like a shell prompt sitting below `tail -f` output. The trick is that Rich's `Console` serialises every write through a single internal lock. No second writer competes for the cursor.

**Why `RichHandler` and `StreamHandler` are not interchangeable**: `StreamHandler` writes a formatted string straight to `sys.stdout` via Python's file I/O. It has no awareness of any terminal control sequences other code might be writing at the same time. When tqdm manages a progress bar with ANSI cursor-move sequences and `StreamHandler` writes a log line, the two streams of ANSI codes interleave and the screen turns to garbage. `RichHandler` doesn't write to stdout directly. It hands the log record to the shared `Console`, which queues the render and applies it at the next safe moment, coordinated with the `Live` panel's own refresh cycle.

**How `Progress.update()` is thread-safe**: Rich's `Progress` object holds an internal `threading.RLock`. Every call to `update()`, `add_task()`, or `advance()` acquires the lock before touching the task list and releases on return. Any number of threads can call `update()` at once without corrupting data; the calls just serialise internally. That's why PlexMigrate's worker threads call `_live_progress.update()` directly with no external synchronisation.

**`requests.adapters.HTTPAdapter` and what "mounting" means**: A `requests.Session` routes each request through an adapter based on URL scheme. `session.mount("http://", adapter)` tells the session to use this adapter for every URL starting with `http://`. The adapter controls connection pooling (how many persistent TCP connections to keep open) and the retry policy. By default, sessions use a basic adapter with no retries and a pool of one connection per host. `HTTPAdapter` with a `Retry` object adds automatic re-sending of failed requests according to the rules in the `Retry` config.

**`urllib3.util.retry.Retry` fields explained**:
- `total=2`: at most 2 retry attempts per request (3 total tries including the original).
- `connect=2`: applies the retry count specifically to connection failures.
- `read=1`: applies a separate retry count to read failures (server accepted the connection but sent no response).
- `backoff_factor=0.5`: sleep `0.5 × 2^(retry_number - 1)` seconds between retries, so 0.5 s then 1 s.
- `status_forcelist=[500, 502, 503, 504]`: only retry on these specific HTTP status codes (server errors and gateway errors). 404 and 401 are not retried.
- `allowed_methods=frozenset(["GET", "PUT"])`: only retry GET and PUT requests. POST is excluded because it isn't idempotent; retrying could submit the same data twice.
- `raise_on_status=False`: after all retries are exhausted, return the error response rather than raising. The caller decides how to handle it.

**`threading.Semaphore` vs `threading.Lock`**: A `Lock` allows exactly one thread at a time. A `Semaphore(n)` allows exactly n threads at a time, a generalisation of a lock where the capacity is configurable. In PlexMigrate, item resolution (reading from Plex's database) safely runs at `MAX_WORKERS` concurrency because it's read-only. Scrobbling (writing viewCount) should be throttled to `SCROBBLE_WORKERS` simultaneous threads so Plex's write queue doesn't get overwhelmed. A semaphore expresses "allow n simultaneous writers" without a separate thread pool or queue.

---

## Sources - v0.5.0 - 2026-05-10

### Source 30 - `dataclasses` module (Python standard library)

**Used for**: `ActivityEntry`, `LibraryProgress`

**Concept**: Python dataclasses (Python 3.7+) are classes whose primary purpose is holding data. The `@dataclass` decorator auto-generates `__init__`, `__repr__`, and `__eq__` from field annotations. For `ActivityEntry` and `LibraryProgress` we declare the fields and their types and the class is usable with zero boilerplate. The `field(default_factory=...)` helper exists because a literal `x: list = []` in a dataclass would share the same list across every instance, a classic Python gotcha.

**Documentation**: https://docs.python.org/3/library/dataclasses.html

**Why this instead of namedtuple**: Both create lightweight record types. Dataclasses support mutable fields, default values, and inheritance, and produce nicer error messages. `namedtuple` is slightly more memory-efficient but less flexible. For a display-only record like `ActivityEntry` the difference is negligible.

---

### Source 31 - `collections.deque` (Python standard library)

**Used for**: `DashboardState.activity`, the live activity feed ring buffer

**Concept**: A `deque(maxlen=8)` is a fixed-size buffer that automatically discards the oldest entry when a new one is appended. No manual pruning, no memory growth. Append is O(1) even at capacity. Regular lists would need either `list.pop(0)` (O(n) because every element shifts) or a separate index, both worse than the built-in behaviour.

**Documentation**: https://docs.python.org/3/library/collections.html#collections.deque

**Why maxlen=8**: Four feed entries are shown in the dashboard at any time. Eight gives a useful scrollback so events aren't immediately overwritten while keeping memory trivial (each entry is a small dataclass).

---

### Source 32 - `threading.Event` (Python standard library)

**Used for**: Pause/resume in `DashboardState`; stop signal for `_keyboard_thread()`

**Concept**: `threading.Event` is a flag that threads can `set()` (true), `clear()` (false), and `wait()` on (block until true). For pause/resume: when the user presses P, `_pause_event.clear()` is called. Worker threads call `wait_if_paused()`, which calls `self._pause_event.wait()`. The wait blocks while the event is cleared and returns the moment it's set. Pressing P again calls `_pause_event.set()`, waking every waiting thread at once. For the keyboard stop signal: a separate `stop_event` is set by Q or when the run finishes, and the keyboard thread exits its loop when `stop_event.is_set()`.

**Documentation**: https://docs.python.org/3/library/threading.html#threading.Event

Unlike a `Lock`, an Event has no ownership. Any thread can set, clear, or wait on it, ideal for broadcast signals like "pause all workers" or "time to exit."

---

## Sources - v0.6.0 - 2026-05-10

### What These Sources Cover

Covers the cross-platform path suffix matching feature and supporting changes in v0.6.0. Documented here: Python's string normalisation, dict-based indexing strategies, and the Plex Web token-based authentication URL format.

| # | Source Name | URL | What It Covers | How It Was Used |
|---|---|---|---|---|
| 33 | Python `str.replace()` and `str.split()` documentation | https://docs.python.org/3/library/stdtypes.html#str.replace | String methods for path normalisation | Used in `_normalize_path_parts()` to replace `\` with `/`, strip drive letters, and split on `/` |
| 34 | Python `str.lower()` documentation | https://docs.python.org/3/library/stdtypes.html#str.lower | Case folding for case-insensitive comparison | Used in `_normalize_path_parts()` to normalise paths to lowercase so `DSOTM` and `dsotm` match |
| 35 | Python `dict.setdefault()` documentation | https://docs.python.org/3/library/stdtypes.html#dict.setdefault | Returns the value for a key if it exists, inserts and returns a default if not | Used in suffix index building: `sfx.setdefault(key, []).append(item)` initialises the list on first use and appends on all subsequent uses without an explicit `if key in dict` check |
| 36 | Python `list` slicing with negative indices | https://docs.python.org/3/library/stdtypes.html#sequence-types-list-tuple-range | `parts[-n:]` returns the last N elements of a list | Used in `_suffix_key()` to extract the last N path components efficiently |
| 37 | Plex Web token authentication URL format | [General knowledge - no public documentation URL] | `/web/index.html?X-Plex-Token=TOKEN` opens the Plex Web UI pre-authenticated | Used in `_open_plex_server()` to construct the auto-login URL for the `[S]` keyboard shortcut |
| 38 | `PlexServer.myPlexUsername` attribute | https://python-plexapi.readthedocs.io/en/latest/modules/server.html | Attribute on a connected PlexServer object that returns the Plex.tv account username | Used in `main()` to get the real account name for log attribution instead of the hard-coded string "Plex Owner" |
| 39 | Python `getattr()` with default | https://docs.python.org/3/library/functions.html#getattr | Returns the named attribute of an object, or a default if the attribute does not exist | Used as `getattr(server, "myPlexUsername", None) or "Plex Owner"` to safely retrieve the account name with a fallback if plexapi ever renames or removes the attribute |

### Concept Explanations

**Path suffix indexing**: A suffix index is a lookup table keyed on the end of a value rather than the beginning or the whole. For file paths, the "end" is the file name and its parent directories, the parts that don't change when a media drive is remounted at a different root. Building the index alongside the main filepath index costs nothing extra (same `section.all()` call, one extra dict insert per item). Lookup is O(1), a single `dict.get(key)` call, versus O(n) for a linear scan over all items. The tradeoff is memory: the suffix index uses roughly 2× the memory of the filepath index alone (two extra dict entries per item for the N=3 and N=2 keys), which is negligible for libraries up to tens of thousands of items.

**Sentinel key pattern**: Storing the suffix index inside `scan_cache` under `"__suffix_index__"` is the sentinel key pattern: pick a key that cannot appear in the data and use it to stash metadata alongside the data in the same container. The double underscore prefix is a Python convention for "private/internal" names and is never a valid file path starting character on any OS, so collision is impossible.

**Path normalisation and cross-platform correctness**: File systems differ across operating systems in three ways relevant here: (1) separator character, `\` on Windows, `/` on Linux/macOS; (2) case sensitivity, Windows NTFS is case-insensitive by default, Linux ext4 is case-sensitive; (3) drive letters, Windows uses `C:\`, Linux uses `/mount/point`. The normalisation in `_normalize_path_parts()` addresses all three: replace `\` with `/`, lowercase everything, strip the Windows drive letter prefix. After normalisation the same physical file produces the same list of path components no matter which OS reported the path.

---

### Source 33 - `concurrent.futures.wait()` with `timeout` (Python standard library)

**Used for**: Display loop polling in `run_snapshot()` and `run_import()` dashboard mode

**Concept**: `concurrent.futures.wait(fs, timeout=0.25)` returns two sets: `(done, not_done)`. It blocks for at most `timeout` seconds. If any futures complete before the timeout it returns early with those futures in `done`; otherwise it returns after `timeout` with `done=set()`. That gives the display loop a 250 ms maximum refresh interval while still processing completions the moment they happen. `as_completed()` would block until the next future finishes (potentially minutes), freezing the dashboard, which is why we use `wait()` with a timeout instead.

**Documentation**: https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.wait

The 0.25 s (4 Hz) timeout was chosen to match the Live panel's effective refresh rate. Going faster (e.g. 0.1 s, 10 Hz) would waste CPU on dashboard rendering when workers rarely produce events that fast. Going slower (e.g. 1 s) makes the dashboard feel unresponsive to worker progress.

---

### Source 34 - `msvcrt.kbhit()` / `msvcrt.getwch()` (Windows-only, Python standard library)

**Used for**: Non-blocking keyboard input on Windows in `_keyboard_thread()`

**Concept**: `msvcrt` is a Python interface to the Microsoft Visual C Runtime. `kbhit()` returns True if a key has been pressed and is waiting in the input buffer, without consuming it. `getwch()` reads and returns one wide character from the console input buffer without echoing it to the terminal. Together they form a non-blocking keyboard read: check if a key is available, consume it if so. The 50 ms `time.sleep()` between checks prevents busy-waiting from consuming a full CPU core.

**Documentation**: https://docs.python.org/3/library/msvcrt.html

**Why not `input()` or `sys.stdin.read()`**: Both of those block until the user presses Enter. The background keyboard thread needs character-at-a-time input without blocking the main thread.

---

### Source 35 - `tty` / `termios` / `select` (Unix-only, Python standard library)

**Used for**: Non-blocking keyboard input on macOS and Linux in `_keyboard_thread()`

**Concept**: On Unix the terminal normally operates in "cooked" (line-buffered) mode, where characters accumulate in a buffer until the user presses Enter and the whole line is delivered. `tty.setraw()` switches the terminal to raw mode, delivering each character immediately without buffering or echoing. `termios.tcgetattr()` / `tcsetattr()` save and restore the original settings. `select.select([sys.stdin], [], [], 0.05)` checks whether stdin has data with a 50 ms timeout; if it does, `sys.stdin.read(1)` reads one character, otherwise the loop iteration is skipped. The `finally` block in `_keyboard_thread()` always restores the original settings, even if the thread is killed by an exception. Leaving the terminal in raw mode after exit would corrupt the shell.

**Documentation**:
- https://docs.python.org/3/library/tty.html
- https://docs.python.org/3/library/termios.html
- https://docs.python.org/3/library/select.html

---

### Source 36 - `subprocess.Popen()` for opening files (Python standard library)

**Used for**: `_open_log_folder()`, opens the log directory in the OS file manager

**Concept**: `subprocess.Popen()` launches a child process and returns immediately, without waiting for the child to finish. The command varies by OS: `explorer <path>` on Windows, `open <path>` on macOS, `xdg-open <path>` on Linux (which delegates to the desktop environment's default file manager). The whole call is wrapped in `try/except Exception: pass` because opening a folder is a best-effort convenience feature. If it fails (no file manager, unusual environment, path doesn't exist), the import keeps running silently.

**Documentation**: https://docs.python.org/3/library/subprocess.html

---

### Source 37 - `rich.panel.Panel` and `rich.text.Text` (third-party)

**Used for**: `_build_dashboard()`, the rendered dashboard panel

**Concept**: `rich.text.Text` is a string with attached style spans. `text.append(string, style="bold green")` adds a segment with a specific colour and formatting. The resulting `Text` object can be passed anywhere Rich accepts a renderable, including inside a `Panel`. `rich.panel.Panel` draws a rounded or straight border around any renderable, optionally with a title. The dashboard uses `Panel(body, border_style="dim", padding=(0, 0))` where `body` is a `Text` built line by line. `padding=(0, 0)` removes the default inner padding so the border sits flush against the content, saving two rows of terminal space.

**Why Text instead of Layout**: `rich.layout.Layout` divides the terminal into named regions and handles proportional sizing. It's more powerful but also more complex and doesn't easily support variable-height regions (one row per library). `Text` built line by line gives full control over every character and style at the cost of manual column alignment. For a dashboard where column widths must be fixed to prevent shimmer, manual alignment is preferable.

**Documentation**:
- https://rich.readthedocs.io/en/latest/text.html
- https://rich.readthedocs.io/en/latest/panel.html

---

### Source 38 - `contextlib.contextmanager` (Python standard library)

**Used for**: `_thread_category()`, registers/unregisters a thread's work category in the dashboard

**Concept**: The `@contextlib.contextmanager` decorator turns a generator function into a context manager. Code before `yield` runs on `__enter__`; code after `yield` (in `finally`) runs on `__exit__`. `_dashboard.unregister_thread()` always runs even if the worker raises, so a crashed thread doesn't stay registered as "active" in the Thread Pool panel forever. Writing context managers as generators is much less boilerplate than implementing `__enter__` and `__exit__` on a class.

**Documentation**: https://docs.python.org/3/library/contextlib.html#contextlib.contextmanager

---

## v0.6.1 Sources

### Source 39 - `str.replace()` chaining for path separator normalisation

**Used for**: fixing remap body separators in `_resolve_item_impl()`

**Concept**: `str.replace(old, new)` returns a new string; it doesn't modify in place. Because it returns a string, you can chain another `.replace()` directly: `s.replace("A:\\", "/mnt/music/", 1).replace("\\", "/")`. The second call converts any remaining backslashes in the body after the root prefix has been swapped. On a path that had no backslashes to begin with (a Linux-sourced path, or one where `old_root` didn't match), the first call returns the original string unchanged and the second call is also a no-op. No branching required.

**Documentation**: https://docs.python.org/3/library/stdtypes.html#str.replace

---

### Source 40 - `plexapi.library.MusicSection.searchTracks()` and `ShowSection.searchEpisodes()`

**Used for**: `_section_leaf_items()`, fetching leaf-level media objects for Music and TV sections

**Concept**: Plex libraries are hierarchical. A Music library contains Artists → Albums → Tracks. `section.all()` returns the top level (Artists). Tracks are the objects carrying `media.parts.file`, the file path stored on disk. `searchTracks()` queries the database for Track objects directly, skipping the Artist and Album levels. TV is the same shape: `section.all()` returns Shows, but `searchEpisodes()` returns Episode objects which have file paths. `searchTracks()` and `searchEpisodes()` both accept the same filter keyword arguments as `section.search()`, but their return types are always leaf-level. For Movies, `section.all()` already returns Movie objects (the leaf level), so no wrapper is needed.

**Documentation**:
- https://python-plexapi.readthedocs.io/en/latest/modules/library.html#plexapi.library.MusicSection.searchTracks
- https://python-plexapi.readthedocs.io/en/latest/modules/library.html#plexapi.library.ShowSection.searchEpisodes

---

## v0.7.0 Sources

No new external dependencies in v0.7.0. Every library and API used (plexapi `PlexServer.playlists()`, `Playlist.create()`, `concurrent.futures`, `Set` from `typing`) was already documented in earlier entries. The key design move, passing `existing_playlists=None` to reuse `import_playlists()` for home users without changing its signature, relies on Python's default-argument fallback pattern. That's part of the language standard and doesn't need a separate source entry.

---

## v0.7.1 Sources

No new external dependencies in v0.7.1. The change is a structural reorganisation using Python's built-in module and package system. Two concepts are central to how the new layout works.

### Python Packages and `__init__.py`

**Used for**: making `services/` importable as `import services.state`, `from services.auth import connect_to_server`, etc.

**Concept**: A Python package is a directory containing an `__init__.py` file. When Python encounters `import services.state` it: (1) looks for a `services/` directory on `sys.path`, (2) checks that `services/__init__.py` exists (marking it as a package), (3) loads `services/state.py` as the `state` submodule. Without `__init__.py`, `import services.state` raises `ModuleNotFoundError`. The `__init__.py` can be empty; its presence is what matters, not its contents.

**Documentation**: https://docs.python.org/3/reference/import.html#package-relative-imports

Python 3.3+ also supports namespace packages that work without `__init__.py`, intended for packages distributed across multiple directories (e.g. plugins). For a self-contained project where every module lives in one place, a regular package with an empty `__init__.py` is simpler and more explicit, so we don't use them.

---

### Python Module Object Indirection for Mutable Globals

**Used for**: `import services.state as state` + `state._dashboard = DashboardState(...)` pattern throughout the service modules.

**Concept**: `from services.state import _dashboard` evaluates `services.state._dashboard` at that moment and binds the local name `_dashboard` to that value (which is `None` at import time). If `main()` later runs `services.state._dashboard = DashboardState(...)`, the local binding in every other module still points to `None`. The reassignment is invisible to them. Accessing the value through the module object (`state._dashboard`) is always live because the module object itself is shared: `import services.state as state` binds `state` to the module object, and reading `state._dashboard` evaluates `services.state.__dict__["_dashboard"]` at access time, so it always reflects the latest assignment.

**Documentation**: https://docs.python.org/3/reference/simple_stmts.html#import (import statement semantics); https://docs.python.org/3/reference/datamodel.html#modules (module objects)

The practical rule: `from services.state import X` is safe when `X` is never reassigned, only mutated in place (e.g. `_lib_successes`, `_log_lock`, `_lib_task_ids`). `state.X` is required when `X` is reassigned after import (e.g. `_dashboard`, `_session`, `MAX_WORKERS`, `_live_progress`, `_plex_owner_name`).

---

## v0.8.0 Sources

Nine new technologies were introduced in v0.8.0 for the optional Docker + FastAPI + React web layer. Each is explained below in the same plain-English style as the rest of this file. The engine itself uses no new dependencies in this release; every entry here is consumed only by code under `server/` or `frontend/`.

### FastAPI (Python)

**Used for**: every HTTP route under `/api/*` and the `/ws/dashboard` WebSocket. `server/app.py` builds a single `FastAPI` application instance; uvicorn imports it as `server.app:app`.

**Concept**: FastAPI is a Python web framework built on top of Starlette (the actual ASGI server-side machinery) and Pydantic (request/response validation). Routes are declared as functions with type-annotated parameters; FastAPI translates the annotations into JSON-Schema, validates inbound bodies against them, and generates an OpenAPI document automatically. WebSocket endpoints are first-class: `@app.websocket("/ws/dashboard")` registers a handler that receives a `WebSocket` object and uses `await ws.receive_text()` / `await ws.send_text()` instead of the request/response cycle.

**Documentation**: https://fastapi.tiangolo.com/

**Why FastAPI**: Flask is synchronous WSGI plus `flask-socketio` for WebSockets; the two halves use different concurrency models and that shows when you try to broadcast from a background task to many sockets. FastAPI is one ASGI stack end to end, so the WebSocket fan-out lives on the same event loop as the HTTP routes with no impedance mismatch. Bare Starlette would also work, but the Pydantic integration on FastAPI eliminates a whole class of "client sent a missing field, route crashed at attribute access" bugs.

---

### uvicorn (Python)

**Used for**: actually running the FastAPI app inside the backend container. The `CMD` in `Dockerfile.backend` is `uvicorn server.app:app --host 0.0.0.0 --port 8000`.

**Concept**: uvicorn is an ASGI server, the asyncio-native equivalent of gunicorn or uWSGI in the WSGI world. It owns the event loop, hands inbound HTTP and WebSocket connections off to the application, and supports graceful shutdown via SIGTERM (`docker compose down` sends SIGTERM, uvicorn drains in-flight requests, exits clean). `uvicorn[standard]` pulls in `httptools` and `uvloop` (Unix only) for a measurable performance boost.

**Documentation**: https://www.uvicorn.dev/

**Why uvicorn**: It's the reference ASGI server, packaged by the same author who maintains Starlette. Hypercorn is comparable but has fewer deployment recipes in the wild. Daphne is more focused on Django Channels. For a single-process FastAPI app served behind nginx, uvicorn is the path of least surprise.

---

### Pydantic v2 (Python)

**Used for**: every request and response schema in `server/models.py` (`SettingsIn`, `SnapshotJobIn`, `ImportJobIn`, `ScheduleIn`, `JobStatusOut`).

**Concept**: Pydantic is a runtime validation library. You declare a class inheriting from `BaseModel` with type-annotated fields; instantiating from a dict (or having FastAPI do so from the request body) validates every field against its type and raises a structured `ValidationError` on mismatch. v2 is a near-total rewrite in Rust that runs roughly 10× faster than v1.

**Documentation**: https://docs.pydantic.dev/2.0/

**Why Pydantic here**: Every CLI flag has to be reachable from the web UI per the v0.8.0 spec. The shortest path from "a flag exists" to "the form validates against it" is to put the flag in a Pydantic model field. FastAPI auto-generates the form-validation responses, the React form sends the field name unchanged, and the schema is the spec.

---

### websockets (Python)

**Used for**: the underlying WebSocket protocol implementation. FastAPI exposes the abstraction; the byte-level frame parsing comes from the `websockets` package.

**Concept**: The WebSocket protocol (RFC 6455) starts as an HTTP/1.1 request with the `Upgrade: websocket` header. The server responds 101 Switching Protocols and the connection becomes a bidirectional binary/text frame stream. The `websockets` package handles the handshake, masking, ping/pong, and close-frame negotiation; uvicorn[standard] bundles it as the default WebSocket implementation.

**Documentation**: https://websockets.readthedocs.io/

**Why pin it explicitly**: `uvicorn[standard]` pulls it in transitively via its `wsproto` / `websockets` extras. We list it in `requirements.txt` explicitly so a developer reading the file can see what's powering the `/ws/dashboard` endpoint without spelunking through uvicorn's extras.

---

### React 18 (JavaScript)

**Used for**: the frontend UI. Every component under `frontend/src/components/` is a React function component.

**Concept**: React maps state to a virtual DOM tree. Components are pure functions of their props plus their own `useState`/`useReducer` hooks. The library diffs the virtual DOM against the actual DOM and patches the smallest set of nodes needed. React 18 added concurrent rendering (`useTransition`, `useDeferredValue`), but the state surface here is small enough that the default synchronous render is fine.

**Documentation**: https://react.dev/

**Why React**: Vue and Svelte are equally capable. React has the deepest IDE / TypeScript tooling story, which matters when "every CLI flag exists as a typed form control" is the spec. `tsc` catches mismatched field names at compile time. Solid is faster but newer; React's ecosystem stability is the right tradeoff for a tool the user will install and forget.

---

### Vite (JavaScript)

**Used for**: bundling the React app. `npm run dev` starts a hot-reloading dev server; `npm run build` produces a content-hashed bundle in `frontend/dist/` that nginx serves.

**Concept**: Vite uses esbuild for the dev server (very fast TypeScript stripping) and rollup for the production build (tree-shaken, code-split, minified). It serves source modules over HTTP during development and lets the browser's native ES modules do the loading, which is why startup is near-instant compared to webpack-based pipelines.

**Documentation**: https://vitejs.dev/

**Why Vite over Next.js**: Next.js bundles a server-side rendering layer plus file-based routing. For a single-page dashboard talking to a same-origin API, both are dead weight in the bundle. Vite's `react` template is the minimal viable build chain for this shape of app.

---

### TypeScript (JavaScript)

**Used for**: type checking the frontend. Every `.tsx` file under `frontend/src/` is type-checked by `tsc` during `npm run build` (see the build script in `frontend/package.json`).

**Concept**: TypeScript is JavaScript with an optional type system that runs at compile time only. The emitted JavaScript carries no runtime type checks. It catches typos in field names, wrong argument shapes, and forgotten null cases at the IDE level long before they'd manifest in the browser.

**Documentation**: https://www.typescriptlang.org/docs/

**Why TypeScript for this UI**: The frontend mirrors the engine's CLI flag set 1:1. Without compile-time field-name checking, every flag-rename in the backend would silently break the form. With TypeScript, the build fails the moment `SnapshotJobIn` and `ExportJobPayload` disagree on a field name.

---

### Nginx (operations)

**Used for**: serving the compiled React bundle and reverse-proxying `/api/*` and `/ws/*` to the backend container. Config in `frontend/nginx.conf`.

**Concept**: Nginx is an event-driven HTTP and reverse-proxy server. The features we use: `try_files $uri $uri/ /index.html` for SPA fallback (so deep links like `/snapshots` resolve to the React shell on hard refresh), `proxy_pass` for the API forward, and `proxy_set_header Upgrade $http_upgrade` for the WebSocket upgrade handshake.

**Documentation**: https://nginx.org/en/docs/

**Why nginx**: Caddy auto-provisions TLS and would be fine for a public-facing deployment, but the backend binds to `127.0.0.1` by design and TLS isn't part of the v0.8.0 spec. Nginx has the smallest image (around 50 MB on Alpine) and the simplest config for the same-host static-plus-proxy shape.

---

### Docker + docker compose (operations)

**Used for**: packaging the backend and frontend into two reproducible images and orchestrating them as a pair. `make docker` is a one-line `docker compose up --build`.

**Concept**: Docker images are layered filesystems built from a `Dockerfile`. Each `RUN`/`COPY`/`ADD` step produces a cached layer; subsequent builds reuse layers up to the first change. `docker compose` reads `docker-compose.yml` and brings up multiple services on a shared bridge network with named volumes or bind mounts.

**Documentation**: https://docs.docker.com/

**Why two containers, not one**: A combined backend+frontend image would couple frontend bundle releases to backend image rebuilds. With two images, the Python deps and the node_modules tree change independently, build caches stay warm, and the deployment fits the standard service-per-concern pattern. The compose file binds both services to `127.0.0.1` so they're reachable only from the host that runs `docker compose up`. There's no auth layer on the backend; exposing it to a LAN with a Plex token in plaintext would be irresponsible.

**Why `host.docker.internal` is added explicitly**: On Linux, Docker doesn't resolve that hostname by default; only Docker Desktop on macOS / Windows does. The `extra_hosts: ["host.docker.internal:host-gateway"]` entry in `docker-compose.yml` papers over the platform difference so the same compose file works on every host. `host-gateway` is the special value that resolves to the bridge gateway IP, which on most setups is `172.17.0.1`.

---

## v0.9.0 Sources

No new external dependencies in v0.9.0. The multi-server registry, direct server-to-server transfer, and the server-name filename prefix are all built from concepts already documented in earlier entries. Two patterns appear in this release that deserve a brief note even though they don't pull in a new library.

### Module-Level Variable Reassignment Across Run Boundaries

**Used for**: prefixing per-run log directories and snapshot filenames with the friendly server name without editing the engine.

**Concept**: `services.state._run_timestamp` is a module-level variable set once at module import time. Both `services.logging_ops.setup_logging` and `services.snapshotter.snapshot_library` read it *lazily*; each call computes `f"run_{state._run_timestamp}"` or `f"{lib}_{state._run_timestamp}.plexbackup.json"` at the moment the filename is built. The job worker takes advantage of this by reassigning `state._run_timestamp` to `f"{server_slug}_{datetime.now().strftime(...)}"` immediately before invoking the engine. Both filename builders read the current value at call time rather than capturing it at import time, so the prefix lands in the right places with zero changes to engine source.

**Documentation**: https://docs.python.org/3/reference/datamodel.html#modules (module objects are mutable namespaces; assigning to `module.attribute` from anywhere is visible to every reader of `module.attribute`).

**Why this matters**: The pattern lets the multi-server layer satisfy "every log file and snapshot filename includes the friendly server name" from the spec *without* touching `services/snapshotter.py` or `services/logging_ops.py`. Reassigning a global on the boundary between consumer and engine is uglier than passing a parameter, but the engine constraint ranks higher than aesthetic purity here.

---

### Preloaded Data Injection via Existing Parameter

**Used for**: making direct server-to-server transfer feasible without duplicating engine code.

**Concept**: `services.importer.import_backup_file` takes an optional `preloaded_data: Optional[dict] = None` parameter. The parameter was added in v0.4.0 so `run_import` could pre-read all backup JSON up front (to compute progress-bar totals) and pass each parsed document to the per-file worker without re-reading from disk. That same parameter lets us hand it any dict shaped like a `.plexbackup.json`, regardless of where the dict came from. The direct-transfer orchestrator builds the dict in memory by calling the snapshot-side primitives directly against the source Plex; the import side has no idea the source wasn't a file.

**Documentation**: not applicable; this is a pattern, not a library. It's a worked example of why optional pre-loaded parameters on otherwise file-driven functions are useful for unforeseen reuse.

**Why this matters**: It's the cleanest way to honour both "no engine logic changes" and "no intermediate file on disk" at the same time. No new entry point needed, no importer refactor; we just exercise a parameter the importer already supports.

---

## v0.9.1 Sources

No new external dependencies in v0.9.1. Two minor patterns from the broader web/UI world were applied for the first time in this codebase and are worth a brief note.

### Plex `/identity` Endpoint for Lightweight Probes

**Used for**: the new 30-second status poll behind the server selector chips and the Servers tab status dots.

**Concept**: A Plex Media Server's `/identity` route returns a small XML document with `machineIdentifier`, `version`, and a few other identity fields. It doesn't list libraries, fetch metadata, or touch the media database; it's intended exactly for what we use it for: cheap reachability checks. Calling it with `X-Plex-Token` validates the token at the same time, so a 200 response means "reachable and authorised" in one round trip.

**Documentation**: https://www.plexopedia.com/plex-media-server/api/server/identity/

**Why this matters**: `test_connection` in the registry calls `connect_to_server` and enumerates libraries. That's correct for the manual Test button, but too heavy to run every 30 seconds against every registered server. The `/identity` ping costs single-digit kilobytes and single-digit milliseconds on a LAN, so 30-second polling stays well under any reasonable API budget. The threading section of the README already warns against background polling that adds API load to Plex servers when nothing is happening; `/identity` is the right shape because it's the cheapest request that still proves "the server is up and the token works."

---

### `<fieldset disabled>` for Form-Section Gating

**Used for**: the JobFormPanel's "lower panels are non-interactive until both servers are selected" requirement.

**Concept**: HTML's `<fieldset disabled>` attribute non-interactively greys out every form control inside the element (inputs, selects, buttons, all of them) without changing the DOM layout or adding pointer-event tricks. The native behaviour matches the spec's "non-interactive until selected" requirement. Browsers also block keyboard tabbing into disabled fieldsets, so screen-reader navigation skips them by default.

**Documentation**: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/fieldset#attr-disabled

**Why this matters**: The alternative, applying `pointer-events: none` plus a click guard on every control, is fragile and easy to miss when adding new controls. Wrapping the lower-panels block in a fieldset that flips its `disabled` attribute by a single React state variable means a future developer who adds a control inside the gated region automatically inherits the gating. The opacity drop stays as a redundant cue (not every browser styles disabled fieldsets identically), but the *interactivity* gate is enforced by the platform.

---

## v0.9.2 Sources

No new external dependencies in v0.9.2. One short design-pattern note worth recording so future readers don't have to re-derive it.

### Two Counter Sets in One Snapshot

**Used for**: the v0.9.2 bug fix for the Current Job header.

**Concept**: `DashboardState.to_dashboard_frame()` carries two related but distinct progress counter sets: the engine-level resolution counters (`completed`, `skipped`, `failed`, `unresolved`, `guid_hits`, `filepath_hits`, `suffix_hits`, `fuzzy_hits`) and the per-library progress counters embedded in the `libraries` array (`lib.completed`, `lib.total`). The engine-level counters are incremented per *matched item*: every call to `_record_success` or `_record_failure` increments them. The per-library counters are incremented per *pipeline phase* via `advance_library`; a library import advances four times for the four phases (Play Count, Playlists, Collections, Ratings) plus once per home-user task. Both counter sets are correct for their own purpose; they answer different questions.

**Why this matters**: A reader who sees `dash.completed = 12` and `sum(lib.completed for lib in dash.libraries) = 36` might assume one is buggy. Neither is; they measure different things. The Run Stats panel correctly shows engine-level counts (matched items). The Libraries section and the Current Job header correctly show per-library progress (phases plus matched items). The v0.9.2 bug was that the header had been reading from the wrong set; the fix was a one-line source switch with no engine change. When adding new top-of-dashboard widgets later, pick the counter set deliberately and document the choice in a comment.

---

## Audit Backfill - 2026-05-11

A read-through of the current codebase against this file turned up a handful of libraries and APIs that are actively in use but never got their own entry. They're grouped below by where they're called and what they do. None of these are new dependencies; this is documentation catching up to code that already shipped.

### Python `asyncio` (standard library)

**Used for**: the entire WebSocket broadcaster in `server/ws.py`. `asyncio.Lock` protects the client set during connect / disconnect / broadcast. `asyncio.Event` is the clean shutdown signal for the tick loop. `asyncio.create_task` spawns the background tick task. `asyncio.wait_for` drives the 250 ms tick cadence. `asyncio.CancelledError` / `asyncio.TimeoutError` are caught in the obvious places.

**Concept**: `asyncio` is Python's standard event loop for cooperative concurrency. Where `threading` runs concurrent code on OS threads (with the GIL gating CPU work), `asyncio` runs concurrent coroutines on a single thread that yields control at `await` points. uvicorn runs the FastAPI app on an asyncio event loop, so any background work the server does (like the WebSocket fan-out) lives on the same loop and gets the same lock-free single-threaded semantics for anything that doesn't `await`.

**Documentation**: https://docs.python.org/3/library/asyncio.html for the top-level overview; the two pages we actually exercise are https://docs.python.org/3/library/asyncio-sync.html (Lock, Event) and https://docs.python.org/3/library/asyncio-task.html (create_task, wait_for).

**Why a separate task instead of a per-request loop**: A FastAPI WebSocket handler runs once per client. If each handler drove its own polling, N clients would produce N polls per tick. The single background task in `WSManager` builds one snapshot per tick and fans it out to every client, so the work scales O(1) in the number of viewers.

---

### Python `os.replace()` for atomic JSON writes (standard library)

**Used for**: every save in `server/persistence.py` (`settings.json`, `schedules.json`, `servers.json`) via the `_atomic_write_json` helper.

**Concept**: `os.replace(src, dst)` is an atomic file-system rename on every supported platform. POSIX provides it via the `rename(2)` syscall, Windows via `MoveFileEx` with `MOVEFILE_REPLACE_EXISTING`. The write pattern: open a sibling `.tmp` file, write the full JSON document, close it, then `os.replace(tmp, dst)`. A crash partway through the write leaves the old file fully intact. A crash after the replace leaves the new file fully intact. No observable in-between state where another reader could see a half-written document.

**Documentation**: https://docs.python.org/3/library/os.html#os.replace

**Why not `open(..., 'w')` directly**: Direct overwrite truncates the file before writing the new contents. If the process dies between the truncate and the final byte being flushed, the file is left empty or half-written. That has bitten enough configuration-file libraries that "write-temp + atomic-rename" is the standard recipe.

---

### Python `uuid.uuid4()` (standard library)

**Used for**: assigning server IDs in `server_registry.add_server`, schedule IDs in `persistence.upsert_schedule`, and job IDs in `jobs.JobQueue._submit`.

**Concept**: `uuid.uuid4()` generates a 122-bit random identifier (six bits go to version and variant fields). Collision probability is so low it's effectively zero at our scale. Even creating a billion servers a day, the chance of a collision in a year stays well under one in a trillion. Using a UUID rather than an auto-incrementing integer lets the registry be merged from multiple sources (exported/imported) without ID conflicts, and the IDs leak no information about creation order.

**Documentation**: https://docs.python.org/3/library/uuid.html#uuid.uuid4

---

### Python `time.perf_counter()` (standard library)

**Used for**: measuring round-trip latency in `server_registry.ping_server`, which feeds the "23 ms" chip next to each server in the UI.

**Concept**: `time.perf_counter()` returns a monotonic high-resolution timer suitable for measuring short durations. Unlike `time.time()` it isn't affected by wall-clock changes (NTP slews, daylight saving), and unlike `time.process_time()` it includes time the process spent sleeping or waiting on I/O. That's exactly what we want when measuring network latency.

**Documentation**: https://docs.python.org/3/library/time.html#time.perf_counter

---

### `fastapi.middleware.cors.CORSMiddleware`

**Used for**: letting the Vite dev server (on `localhost:5173`) call the backend (on `localhost:8000`) during development. In production both run behind the same nginx vhost so CORS doesn't apply, but the middleware is harmless there.

**Concept**: A CORS middleware intercepts every response and adds the `Access-Control-Allow-Origin` (and friends) headers that browsers require before they hand a cross-origin fetch result to JavaScript. Without it, the browser refuses the dev-mode fetches and the React app shows a blank page.

**Documentation**: https://fastapi.tiangolo.com/tutorial/cors/

**Why `allow_origins=["*"]`**: Acceptable because the backend binds to `127.0.0.1` (see Security notes in the README) and isn't reachable from anywhere else. If the backend ever gets exposed to a LAN, tighten this to the specific origin(s) you want to allow.

---

### `fastapi.responses.FileResponse`

**Used for**: streaming `.plexbackup.json` files to the browser via `GET /api/snapshots/{file_name}`.

**Concept**: `FileResponse(path=..., filename=..., media_type=...)` streams the file from disk in chunks and sets `Content-Disposition: attachment; filename="..."` so the browser saves the file instead of trying to render it. Streaming matters for large library snapshots that can hit hundreds of MB. Loading the whole file into memory first would blow up RAM under load.

**Documentation**: https://fastapi.tiangolo.com/advanced/custom-response/#fileresponse

---

### `python-multipart`

**Used for**: pulled in by `requirements.txt` because FastAPI emits a warning at import time if it can't find a multipart parser. We don't actually parse multipart bodies anywhere (every endpoint takes JSON), but adding the dependency is the path of least resistance to silence the warning and stay ready for a future endpoint that needs it (e.g. file upload).

**Documentation**: https://andrew-d.github.io/python-multipart/

**Why it's in requirements.txt even though no code imports it**: A FastAPI app that defines no `Form(...)` or `UploadFile` parameters never reaches the multipart parser at runtime, but the import-time scan during route registration still asks for it. Adding it to the pin list silences the warning and keeps the install reproducible.

---

### Browser `fetch()` API

**Used for**: every REST call in `frontend/src/api.ts`. The `http<T>()` helper at the top of that file wraps `fetch(path, init)` with JSON serialisation, response-status checking, and error-message extraction.

**Concept**: `fetch()` is the standard browser-native Promise-based HTTP client, available everywhere we target. It returns a `Response` object whose `.ok` is true for 2xx status codes and whose `.json()` / `.text()` methods read the body. We picked it over `axios` to avoid pulling another dependency into the bundle; the difference for our shape of calls is one short helper function.

**Documentation**: https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API

---

### Browser `WebSocket` API

**Used for**: the live dashboard subscription in `DashboardWsClient` (also in `frontend/src/api.ts`). One `new WebSocket(url)` per page lifetime, with `onmessage` / `onclose` / `onerror` handlers and a reconnect loop with linear backoff.

**Concept**: The browser-native WebSocket constructor takes a `ws://` or `wss://` URL and returns an object that emits events when the connection opens, receives a frame, errors, or closes. There's no built-in reconnect. We add one in `DashboardWsClient.ensureConnected()` with 1-to-10-second backoff so a backend restart doesn't leave the dashboard frozen.

**Documentation**: https://developer.mozilla.org/en-US/docs/Web/API/WebSocket

**Why same-origin (`wss://${window.location.host}/ws/dashboard`)**: Same-origin URLs mean the browser sends cookies and respects any CORS / SOP gating naturally, and nginx in the frontend container forwards the upgrade to the backend with `proxy_set_header Upgrade $http_upgrade` (see `frontend/nginx.conf`).

---

### Browser timer APIs: `setInterval` / `clearInterval` / `setTimeout`

**Used for**: the 30-second polling loops in `ServersPanel` and `JobFormPanel`, and the per-second re-render trigger in `DashboardPanel` (so elapsed-time strings keep ticking when no new WebSocket frame has arrived).

**Concept**: The standard browser timer functions. `window.setInterval(fn, ms)` calls `fn` every `ms` milliseconds until the returned handle is passed to `clearInterval`. The React pattern: open the timer in a `useEffect`, return a cleanup function that clears it on unmount.

**Documentation**: https://developer.mozilla.org/en-US/docs/Web/API/setInterval

---

### Docker base images we pin to

The two Dockerfiles in this repo build on three official images. Each is documented on Docker Hub and worth knowing about:

| Image | Used in | Documentation |
|---|---|---|
| `python:3.11-slim` | `Dockerfile.backend` (single stage) | https://hub.docker.com/_/python |
| `node:20-alpine` | `frontend/Dockerfile` (build stage) | https://hub.docker.com/_/node |
| `nginx:alpine` | `frontend/Dockerfile` (serve stage) | https://hub.docker.com/_/nginx |

**Why `slim` and `alpine` flavours**: Both strip the base OS down to the bare runtime. `python:3.11-slim` is Debian without `build-essential` etc.; `:alpine` variants use musl libc on a minimal Alpine Linux base. Combined image size for backend + frontend lands around 250 MB; the default `python:3.11` and `node:20` would be closer to 1.5 GB.

**Why pin to a major version, not `latest`**: `latest` floats forward and can move from under us during a build. Pinning to `3.11-slim` / `20-alpine` / `alpine` keeps the build reproducible across machines for the lifetime of the major release without locking us to a specific patch.

---

### GNU Make

**Used for**: the `Makefile` at the project root. `make docker`, `make cli`, `make clean`, `make help`.

**Concept**: Make is a build automation tool that's been the lowest-common-denominator for "type one short command, run a known recipe" for forty years. We don't use it for actual build dependency tracking (the targets are phony, declared `.PHONY`); it's a convenient command launcher that works on macOS, Linux, and Windows with WSL or Git Bash.

**Documentation**: https://www.gnu.org/software/make/manual/make.html

**Why Make and not a shell script**: The targets have logical names (`docker`, `cli`) that read better than `./scripts/docker.sh`. Make is preinstalled on macOS and every Linux distro we'd realistically run this on; Windows users running everything else here through Docker Desktop have Git Bash, which ships GNU Make.

---

## PR-13 Sources - Snapshot pipeline + retention

Added during the export -> snapshot rename + capture pipeline (Commit A/B/C). The bulk of the code reuses existing patterns already documented above; this section calls out only the new external API surface PR-13 introduced.

### `sqlite3.Connection.backup()` and ATTACH DATABASE

**Used for**: `server/snapshot_capture.py :: create_snapshot_db`. The
function attaches the live `media.db` to a freshly-created snapshot
database via `ATTACH DATABASE` and runs filtered `INSERT ... SELECT`
queries against the attached schema. ATTACH lets one connection
read from two database files in the same query, which is the
cleanest way to filter-and-copy without staging intermediate data
in Python.

**Concept**: `ATTACH DATABASE 'path' AS alias` exposes the second
file's tables under the `alias.tablename` namespace within the
current connection. Cross-database queries work normally
(`INSERT INTO main.foo SELECT * FROM src.foo WHERE ...`). The
attached database closes on `DETACH DATABASE alias` or when the
connection itself closes.

**Documentation**:
* https://www.sqlite.org/lang_attach.html
* https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup

**Why not `Connection.backup()`**: The online backup API copies the
*entire* source database byte-for-byte. PR-13 snapshots are scoped
to one `server_id`; a full backup would carry every server's data
into every snapshot. ATTACH + filtered INSERT is the only way to
project a subset.

**Why `?mode=ro` URI**: The source connection is opened with the
SQLite URI form `file:path?mode=ro` so a concurrent engine writer
on `media.db` doesn't fight us for the WAL. Read-only mode also
guarantees the snapshot can't accidentally mutate the live store.

---

### `ATTACH DATABASE` lock interactions with WAL

**Used for**: the same `snapshot_capture.create_snapshot_db` path -
relevant because snapshots are sometimes captured while an engine
job is mid-write to `media.db`.

**Concept**: WAL journal mode (set on `media.db` at init time)
allows one writer and any number of readers to coexist. An ATTACH
of a WAL database from a separate connection sees a consistent
point-in-time view through the WAL even if the writer commits new
pages during the read. The PR-13 capture pipeline depends on this
behaviour.

**Documentation**: https://www.sqlite.org/wal.html (section
"Concurrency")

---

### Fernet symmetric encryption (cryptography library)

**Used for**: existing in `server/secrets.py` since v0.9.5 for the
Plex-server-token at-rest encryption. PR-10 extended it to encrypt
per-managed-user credentials (token, Plex Home PIN, service
password) stored in `media.db.managed_users`. PR-13's snapshot
capture explicitly *excludes* the `managed_users` table from
snapshot `.db` files because Fernet ciphertext is bound to this
host's `.keyfile` and would be unreadable if the snapshot were
restored on a different host.

**Documentation**: https://cryptography.io/en/latest/fernet/

**Why exclude credentials from snapshots**: Restorability across
hosts is a stated PR-13 goal. Carrying ciphertext bound to one
host's keyfile would break that. PR-11's sync re-populates the
table from the live API after a restore - credentials are then
re-entered by the operator via User Management.

