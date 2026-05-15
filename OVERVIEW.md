# PlexBackUp - Technical Overview

This document is the **architectural reference** for PlexBackUp. It covers the technical decisions behind the engine, the databases, the API surface, the concurrency model, and the tradeoffs that come with each choice. It also collects the advanced setup and security topics that operators don't need to read to get the tool running.

> **Looking for what PlexBackUp is, why it exists, or how to use it?** See [README.md](README.md), the operator manual. **Want a 5-minute setup walkthrough?** See [QUICKSTART.md](QUICKSTART.md).

---

## What this document covers

If you came here from a pointer in the README, the section you want is probably one of these:

**Architecture and design decisions**

* [High-level architecture](#high-level-architecture) - the engine, the web layer, and the SQLite trio
* [Why GUIDs, not ratingKeys](#why-guids-not-ratingkeys) - the keying decision that makes snapshots portable
* [The snapshot lifecycle](#the-snapshot-lifecycle) - what happens from "Submit" to "Completed"
* [Restore: Merge, Replace, and the safety belt](#restore-merge-replace-and-the-safety-belt)
* [Direct transfer](#direct-transfer) - in-memory transfer and the per-library fallback
* [Fan-out coordination](#fan-out-coordination) - parallel writes to multiple destinations
* [The live dashboard data flow](#the-live-dashboard-data-flow) - the 4 Hz WebSocket pipeline
* [Databases](#databases) - why three, why WAL, why a single writer
* [The Plex API contact surface](#the-plex-api-contact-surface) - retry policy, throttling, telemetry
* [Managed user authentication](#managed-user-authentication) - parallel token-or-PIN auth and the preflight check
* [Concurrency, Python, and the GIL](#concurrency-python-and-the-gil)
* [Why a single-worker engine](#why-a-single-worker-engine)
* [Design tradeoffs and known sharp edges](#design-tradeoffs-and-known-sharp-edges)
* [Status and roadmap](#status-and-roadmap)
* [Where to read the code](#where-to-read-the-code)

**Advanced setup and operations (referenced from the README)**

* [Security architecture](#security-architecture) - at-rest encryption, JWT auth, log scrubber, file permissions
* [Exposing PlexBackUp to other devices on your network](#exposing-plexbackup-to-other-devices-on-your-network) - the optional LAN-exposure walkthrough
* [Migration from v0.8.0](#migration-from-v080) - what happens to your old settings and schedules on upgrade

---

## High-level architecture

PlexBackUp is a FastAPI backend with a React frontend, running typically in Docker. It communicates with Plex through `plexapi` and a shared `requests.Session` that centralises retry and throttling behaviour.

Core pieces:

* **The engine** (`services/`) - one shared body of code that knows how to talk to Plex, gather metadata, resolve items, and write data back. Both the CLI driver (`plexmigrate.py`) and the FastAPI server call into it.
* **The web layer** (`server/` FastAPI backend + `frontend/` React UI) - wraps the engine in a single-worker job queue, a multi-user auth layer, a live WebSocket dashboard, scheduling, log browsing, and snapshot browsing.
* **The SQLite trio** (`auth.db`, `media.db`, `snapshots.db` under `server_data/`) - three deliberately separated stores with different ownership patterns.

The defining architectural choice: the data model is consciously **Plex-agnostic at rest** and Plex-specific only at the contact surface. Every per-row table in `media.db` (`server_items`, `watch_events`, `ratings`, `playlists`, `collections`, and their join tables) carries a `backend` column, and the matcher keys on global identifiers (IMDb, TMDB, TVDB, MusicBrainz GUIDs) rather than Plex's own `ratingKey`. That single decision is what makes future Emby and Jellyfin support a "write two new primitives" task rather than a rewrite.

---

## Why GUIDs, not ratingKeys

Plex's internal `ratingKey` is **ephemeral**. It is unique inside one Plex install at one point in time. Rebuild the database, or move to a new server, and every `ratingKey` changes. Snapshots that referenced those keys would be useless after a rebuild, which is exactly the failure mode PlexBackUp exists to prevent.

PlexBackUp keys on upstream metadata identifiers instead:

* `imdb://tt0133093` (The Matrix)
* `tmdb://1399` (Game of Thrones)
* `tvdb://121361`
* `mbid://b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d` (a MusicBrainz GUID)

These identifiers survive every rebuild, every cross-server move, and every cross-platform move. An Emby item and a Plex item pointing at the same IMDb GUID land on the same logical row in the working pool. That is what "backend-agnostic at rest" means in practice.

The four-tier matcher in `services/resolver.py` falls back through GUID, exact filepath, path-suffix (for cross-platform migrations where the root is different but the tail of the path matches), and finally fuzzy title comparison. Failures from each tier are recorded with the specific reason, so a per-library `_fail` log can tell you whether something missed because the GUID isn't in the destination library yet or because the title is ambiguous.

---

## The snapshot lifecycle

When the operator clicks **Submit** on a snapshot job, here is the full path:

1. **HTTP validation.** `POST /api/job/snapshot` is validated against a Pydantic model (`SnapshotJobIn`). Legacy flag names from older releases are normalised into the modern `include_*` fields by a `model_validator`, so old clients still work.
2. **Preflight.** A separate endpoint (`POST /api/job/preflight-pin-check`) is called first by the frontend. If any managed users have a Plex Home PIN set with no captured token, the response lists them and the UI shows the "PIN-protected users detected" modal. The operator either acknowledges and continues, or cancels and fixes the missing PINs first.
3. **Enqueue.** A `JobRecord` is appended to the in-process FIFO queue. **Only one engine call runs at a time** (see "Why a single-worker engine" below).
4. **Worker picks it up.** The job worker decrypts the host's Plex token from `servers.json` (Fernet, see `server/secrets.py`), opens a `PlexServer` connection through `plexapi`, and builds a per-run logger that writes into `plex_logs/run_<slug>_<timestamp>/`.
5. **Engine entry.** `run_snapshot()` resets per-run counters, applies library/user filters, and decides whether to run the owner phase, the per-user phase, or both.
6. **Playlist prebuild.** An 8-worker pool warms the playlist cache for all included users in a single burst. This reduces playlist gather API calls from O(libraries x users) to O(unique users).
7. **Per-library dispatch.** A `ThreadPoolExecutor` (default 16 workers) calls `snapshot_library()` once per included library. Crucially, the submit is wrapped by `submit_with_context()` (see `services/dashboard.py`), which captures the calling `ContextVar`s and re-establishes them inside the worker. This is what makes fan-out destination isolation work.
8. **Inside one library.** Each `snapshot_library()` call has an **owner phase** (concurrent fetch of watch history, ratings, playlists, and collections using the server-owner token) and a **per-user phase** (the same, repeated once per managed user with their own token). The per-user collection pass uses three-layer optimisation (early exit, skip-before-work, optional fast owner detection) so library-wide collections are not paid for N times.
9. **Payload accumulation.** Each library appends a per-library payload dict to a thread-safe global accumulator.
10. **Finalize.** The engine creates a fresh per-server snapshot `.db` file, borrows the schema from `media.db`, inserts every captured row inside one transaction (much faster than per-row commits), writes a `meta` block with the timestamp and library set, and registers the new file in `snapshots.db`.
11. **Terminal state.** The job reaches `STATE_COMPLETED` on success or one of the failure states on error. The final dashboard frame is captured so elapsed time and ETA freeze at the actual run duration.

---

## Restore: Merge, Replace, and the safety belt

Restore is opinionated about data ownership. Two top-level modes:

| Mode | Semantic | Confirmation | Safety belt |
|---|---|---|---|
| **Merge** (default) | Strictly additive. Watch counts take the higher value, ratings already set are left alone, playlists and collections gain missing members but never lose any. Idempotent. | None. | None needed - nothing is removed. |
| **Replace** | Makes the destination match the snapshot exactly. Watch counts re-scrobbled to the snapshot value, ratings overwritten, playlists and collections recreated from scratch. | Type the word `REPLACE` in the UI. | A pre-replace snapshot is captured automatically. If the safety belt fails to capture, the destructive write is **not** allowed to start. |

The four-tier matcher runs the same way in both modes. Failure reasons from each tier land in the per-library `_fail` log so a missed item can be traced back to whether its GUID wasn't in the destination library, whether the filepath remap was wrong, or whether the title was ambiguous.

**The safety belt is the contract.** A Replace job that successfully starts has, by construction, a captured pre-state snapshot to roll back from. The engine refuses to fire the destructive write otherwise. This is why operators can use Replace mode without ceremony - the worst case is "restore from the safety belt".

---

## Direct transfer

Direct transfer is "snapshot into memory + restore from memory" packaged as one job. No JSON or `.db` file touches disk on the happy path. The engine pulls a library from the source, hands the in-memory payload directly into the restore code path against the destination, and proceeds to the next library.

If the direct path fails for a given library (a network blip, an OOM on a very large library, an unexpected API response), the engine falls back automatically to a **chained path for that library only**: write a temporary `.tmp.plexexport.json`, restore from it, delete the temp on success. Sibling libraries keep going on the direct path. The dashboard activity feed notes the fallback so it is auditable.

The "per library" granularity matters. A naive design would either abort the whole job on the first error or fall back the whole job, both of which produce worse operator outcomes than mixed-path success.

---

## Fan-out coordination

Fan-out is "one snapshot or one direct-transfer write to multiple destination servers in parallel". When you select more than one destination in the Run Job form, the engine spawns one worker thread per destination using its own `ThreadPoolExecutor`.

The trick that makes this safe:

* Each destination thread sets its own per-run context (`active_plex_url`, `active_plex_token`, `active_owner`, `active_dashboard`, per-library accumulators) using `contextvars.ContextVar`.
* Sub-pools submitted from inside a destination thread inherit that context via the `submit_with_context()` helper.
* Every HTTP call, log write, and dashboard counter mutation reads from the current `ContextVar`, so even though several destinations are running in parallel, their state is fully isolated.

A destination that fails takes only its own card on the dashboard down. Siblings keep running. The safety belt fires **per destination, not per fan-out**, so each destination has its own rollback snapshot. Per-user filtering and remap-path apply to every destination in the job.

Currently, three of the run-level log files (`runtime.log`, `errors.log`, `media.log`) are still shared across destinations of one fan-out because the engine attaches `FileHandler`s to named loggers. A future release will route these through a context-aware filter so each destination's log directory contains only its own records. The per-library `_success` / `_fail` / `troubleshoot.log` files are already correctly isolated.

---

## The live dashboard data flow

The 4 Hz WebSocket dashboard is the part of PlexBackUp that feels alive. The data flow:

1. **Mutation.** A worker thread increments a counter or appends a line to the activity feed by calling a method on the dashboard handle in its current `ContextVar`. The handle holds an internal lock so concurrent writes from sibling threads don't race.
2. **Broadcaster.** A separate thread, started by `server/ws.py`, ticks at 4 Hz. On each tick it captures a `DashboardState` snapshot, builds a JSON frame via `DashboardState.to_dashboard_frame()`, and sends it to every connected WebSocket client.
3. **Browser.** The React WebSocket handler pushes the frame into the dashboard's state store. React re-renders the affected panels.

The 4 Hz cadence is deliberate. Lower than that and the dashboard feels laggy; higher and you saturate the browser's render loop on slow machines without giving the operator any real new information.

This is also the path that surfaces Plex API health to the operator. A response hook on the shared `requests.Session` captures every 429 status code and the `Retry-After` header into the dashboard's HTTP telemetry panel. If 429s climb during a run, the operator can see it in real time and drop the worker count.

---

## Databases

PlexBackUp keeps state in three SQLite files under `server_data/`. Each one has a distinct responsibility, which is the architectural reason any one of them can fail or be rebuilt without taking the others down.

| File | What it stores | Sensitivity | Write cadence |
|---|---|---|---|
| `auth.db` | App user accounts, password hashes (bcrypt), JWT refresh tokens. Nothing about Plex. | High - bypassing this bypasses the UI's login layer. | Low - changes when users are added or rotate passwords. |
| `media.db` | The live working pool: every metadata item, watch event, rating, playlist, and collection, keyed by global GUIDs. | Medium - leaks library contents but no credentials. | High during a snapshot or restore; otherwise idle. |
| `snapshots.db` | An index of every captured snapshot `.db` file with per-server retention metadata. | Low - pointers and counts. Can be rebuilt from disk. | One row per snapshot capture. |

### Why three databases and not one

Operationally, this split lets you:

* Scrub `media.db` and rebuild it from the snapshot files on disk.
* Delete `snapshots.db` and have PlexBackUp regenerate it by walking the `snapshots/` directory on next boot.
* Back up `auth.db` independently on its own cadence, because it changes for different reasons than the others.

Architecturally, it matches the **failure isolation** principle. A corrupted `media.db` should never lock you out of the UI. A lost `snapshots.db` should never lose your captured data. A wiped `auth.db` should not destroy your library state.

### Why WAL mode and a single in-process writer lock

Every database uses SQLite's **WAL** (write-ahead logging) journal mode. WAL allows concurrent readers without blocking writers and vice versa, which is what makes the dashboard polling and the engine writing coexist cleanly during a long capture.

There is **one in-process writer lock per database**, taken at the application level. This is simpler than a connection pool and sufficient because:

* Writes are infrequent compared to reads (one transaction per snapshot, plus occasional UI mutations).
* The cost of a queued writer waiting briefly is negligible.
* Avoiding a connection pool removes an entire class of "stale connection" bugs.

If write volume ever climbs significantly (for example, if the per-user PIN auth state started churning), this is the first thing to revisit.

---

## The Plex API contact surface

Every call PlexBackUp makes to Plex goes through one shared `requests.Session` built in `services/auth.py`. The session is mounted on both PlexBackUp's own HTTP client **and** `plexapi`'s internal session, so `getByGuid()`, `section.search()`, `section.all()`, and any direct REST call share the same policy.

### Retry policy

```python
urllib3.Retry(
    total=4,
    status_forcelist=[429, 500, 502, 503, 504],
    backoff_factor=0.5,
    respect_retry_after_header=True,
)
```

The `respect_retry_after_header=True` part is the load-bearing line. Plex's 429 responses carry a `Retry-After` header that tells the client when to come back. PlexBackUp obeys it instead of pounding the server until it works. The `backoff_factor=0.5` is used as the fallback when there is no header, producing exponential backoff with jitter at 0.5, 1.0, 2.0, and 4.0 second waits.

### Pool sizing

Pool connections and pool max size are tunables (defaults 4 and 10). Set too low, concurrent calls starve each other. Set too high, file descriptors are wasted on idle sockets. The defaults are tuned for the default 16-worker pool.

### Telemetry hook

A response hook captures 429 status codes and `Retry-After` header values into the dashboard's HTTP telemetry panel. This is the operator-visible signal that Plex is throttling and how hard.

### Managed user authentication

`get_home_users()` parallel-authenticates every included managed user in a single `ThreadPoolExecutor` burst:

1. Try token-based auth using a token captured during the most recent server-add or background sweep.
2. If that fails, fall back to PIN-based auth using the encrypted PIN in the `managed_users` table.
3. If both fail, the user is **dropped from the run with a clear log line**. The engine does not silently fall through to admin-token impersonation.

The "no silent admin impersonation" behaviour fixed a bug from earlier prereleases where users without captured tokens or PINs were still being processed using the admin token, producing incomplete data (the admin's view of a user's library, missing anything PIN-scoped). The user-visible artifact of the fix is the **preflight modal** that lists at-risk users before the job submits.

---

## Concurrency, Python, and the GIL

PlexBackUp uses Python threads not despite the GIL but in recognition of the workload. To make it concrete: in a typical 4-library snapshot, the engine spends roughly **99% of its wall-clock time waiting** for Plex HTTP responses (gather phase) or for SQLite fsync (capture phase). Both of those waits **release** the GIL. A `ThreadPoolExecutor(max_workers=16)` against Plex is genuinely 16x parallel for the part that matters.

What the GIL **would** hurt is CPU-bound work like image resizing, complex regex on huge strings, or deserialising multi-gigabyte JSON. PlexBackUp doesn't have any of that on a hot path. The serializer in `server/snapshot_serializer.py` would warrant attention if a snapshot ballooned past a few hundred MB, but that is far beyond any real library.

The deeper point: the choice that matters isn't threads vs asyncio vs multiprocessing. It is **I/O concurrency vs serial I/O**. Once you have decided to fire 16 Plex requests at once, the choice of how you wait on them is mostly stylistic. Threads with `requests` are easier to read for new contributors than `asyncio` with `httpx`, so threads it is.

Two related decisions in the same vein:

* **`ContextVars` over `threading.local`.** Fan-out spawns one thread per destination, and each library inside a destination submits work to a sub-pool. `threading.local` would not propagate across that submit boundary; `ContextVar`s do, via `submit_with_context()`. That single design decision is what makes the multi-destination dashboard work without races.
* **WAL-mode SQLite with one writer.** Already covered above. Readers never block writers and vice versa, and writers are serialised by a single in-process lock per database - simpler than a pool, sufficient for the write volume.

---

## Why a single-worker engine

Only one engine call runs at a time. Jobs from the web UI queue up behind each other in a single-worker `JobQueue`. This is a deliberate trade-off:

* The engine maintains shared in-memory state (counters, the dashboard handle, the per-run logger, accumulators).
* Serialising the engine is far simpler and safer than making every internal global thread-safe.
* It also matches what most operators actually want: one job per server at a time. Two concurrent snapshots against the same Plex server would compete for API budget anyway and produce a worse outcome than running them in sequence.

The cost is throughput when many jobs queue up at once. The win is a simpler, more debuggable engine with one obvious place to look when something goes wrong. If true concurrent engine execution ever becomes important, the path forward is either a per-call state-isolation pass (move all the per-run globals behind `ContextVar`s the same way fan-out does for destinations) or a process-per-job model.

Inside a single engine call, work is heavily parallelised - across libraries, across users within a library, across items within a per-user collection pass. The serial boundary is only at the job level.

---

## Design tradeoffs and known sharp edges

The kind of thing a careful reviewer would call out. Operators do not need to act on any of these; they are listed so a contributor reading the code knows what to be careful with.

* **Centralised state module.** `services/state.py` holds the per-run context that everything in `services/` reads from. Fan-out works only because that state is per-`ContextVar`, not per-thread-local. Anyone touching `state.py` should run the fan-out integration tests in `tests_backend/` to make sure isolation isn't accidentally broken.
* **Single-worker engine.** Covered above. Conscious choice, not a Python limitation.
* **JSON sidecars are derived, not authoritative.** The snapshot `.db` is the source of truth. The `.plexexport.json` sidecars are generated on demand from the `.db` for portability and inspection. If the two ever diverge, trust the `.db`.
* **Merge mode's "sum" semantic** on watch counts can be a footgun when combined with a scheduled job whose source counts climb between captures. The destination counts will keep climbing too. That is by design (Merge means "take the higher value"), but it surprises operators who expect "Merge" to mean "additive once". Use Replace mode if you need exact equality.
* **Shared run-level log files in fan-out.** `runtime.log`, `errors.log`, `media.log` are currently shared across destinations of one fan-out job. The per-library logs and `troubleshoot.log` are correctly isolated. The shared streams are fixable with a context-aware logging filter; until then, treat them as "aggregate run streams" rather than "per-destination streams".
* **Audit log discipline.** Every restore and direct-transfer job writes its intent (mode, scope, actor, target) to the run log before any write fires. The log scrubber strips credentials from every log record going through Python's `logging` framework. Anything the engine did is reconstructable from logs.

---

## Status and roadmap

Today PlexBackUp is focused on Plex, but the model is explicitly backend-agnostic. Every media row in `media.db` carries a `backend` field. The engine is structured so that gather/restore primitives are swappable per backend. Emby and Jellyfin can be added without rewriting the core reconciliation logic.

What's still needed for a new backend is two pieces of code, not a rewrite:

1. **A gather primitive** that knows how to read watch history, ratings, playlists, and collections from the platform's API.
2. **A restore primitive** that knows how to write those same things back.

Everything else (the snapshot file format, the four-tier matcher, the dashboard, the multi-user UI, the fan-out engine, the safety belt, the scheduling system, the auth layer, the retry policy) is already platform-agnostic.

Planned directions:

* First-class **Emby and Jellyfin** support.
* More granular **job controls and partial-restore options**.
* **Advanced scheduling and policy-based merge strategies** (for example, "merge watch counts but Replace ratings", or "merge only items added in the last 30 days").

There is no committed milestone for the multi-backend work yet; it is a near-term direction rather than a deadline.

---

## Security architecture

The web UI's basics (always-on login, encrypted tokens at rest) are described in the README. This section is the full picture for anyone who wants to understand what the security posture actually is.

### Auth layer (always on)

* The backend has **always-on multi-user authentication as of PR-A2**. Previously it was opt-in via `PLEXMIGRATE_AUTH_ENABLED=true`; that env var has been removed. There is no fallback flag to disable auth.
* On first boot the web UI walks you through creating a root admin account; every subsequent boot goes to the login screen.
* Every API call and the WebSocket require a JWT issued by `/api/auth/login`.
* **Upgrade note:** on first boot after upgrading to PR-A2, you'll be prompted to log in with your existing admin credentials.

### Tokens encrypted at rest (v0.9.5+)

* A 256-bit Fernet key is generated on first boot and stored at `server_data/.keyfile` (raw bytes, mode `0o600`).
* Every Plex token in `servers.json` and the legacy `settings.json` is encrypted with that key; on disk you'll see `gAAAAAB...` ciphertexts rather than the raw tokens, and each row carries an `"_encrypted": true` marker.
* If the keyfile is deleted or replaced, existing encrypted tokens become unrecoverable. The operator gets an actionable "re-enter credentials" message in the UI rather than a crash.
* Decryption happens only at the point a token is handed to plexapi; the plaintext never lands in any log line, API response, or export file.

### Log scrubber

A log-scrubber filter strips credentials from every record written through Python's logging framework. It covers:

* The Plex token (`X-Plex-Token`)
* The plex.tv login token (`authToken`)
* The JWT (`access_token`, `?token=`)
* Any `password` field
* Fernet ciphertext blobs

So plexapi exceptions whose message includes a token-bearing URL don't leak it into `runtime.log`, `errors.log`, or Docker's stdout. The job worker's traceback path was rerouted from `traceback.print_exc()` (which bypassed handlers) through `logger.error(..., exc_info=True)` so the scrubber catches it too.

### API surface

* The Settings and Servers API endpoints return `""` (or `has_token: true/false`) for the token field, never the value itself.
* The Pydantic models reject Windows host paths in `output_dir` / `log_dir` so a misconfigured run can't silently write into the container's ephemeral filesystem.

### File permissions (the Windows caveat)

Credential-bearing files under `server_data/` (`settings.json`, `servers.json`, `media.db`, `.keyfile`, `.auth_secret`) are created with mode `0o600`.

**On Windows this mode bit is ignored.** `os.chmod` does not produce a restrictive ACL. So on a Windows host the `server_data/` **directory ACL is the actual security boundary** and must be locked down to the service account. Protect `server_data/` the same way you protect any other server credentials directory.

---

## Exposing PlexBackUp to other devices on your network

Out of the box, PlexBackUp is reachable only from the Docker host. If you want to run the web UI from a **phone, tablet, or another desktop** on your home network without setting up a full reverse proxy, there's a two-step path: turn on the built-in auth layer, then expose the frontend port. This walkthrough does both.

**Strong recommendation: enable auth FIRST, port-bind SECOND.** Exposing the frontend to your LAN without authentication means anyone on the same network can open the UI, register your Plex servers, dump your watch history, or worse. The auth layer adds a login screen, gates every API call on a JWT, and stays passive when you don't need it. There is no good reason to do this in the opposite order.

**Pros of exposing the UI to your LAN:**

* Submit and watch jobs from any device, useful when a long fan-out is running on your headless server and you'd rather check on it from the couch.
* Operate the UI from a screen larger than the host's (e.g. tablet on a desk while the Docker host is a NUC under the TV).
* Multiple people in the household can have their own logins (use `POST /api/auth/users` from an admin account to create operator-role accounts).

**Cons / things to know:**

* The backend holds Plex auth tokens. Even with the login layer in front, you're exposing more attack surface than the loopback-only default. Don't expose to networks you don't control: guest Wi-Fi, public Wi-Fi, corporate networks, and so on.
* JWT secrets and bcrypt password hashes both live under `server_data/`. Anyone with read access to that directory bypasses the login layer entirely. The bind-mounted volume should have host-level permissions matching the trust level of the LAN you're exposing to.
* The backend has no rate-limiting on the login endpoint. A determined attacker on your LAN with weeks of time could brute-force a short password. Use a strong one (12+ random characters or a four-word passphrase).
* This setup doesn't get you remote access from outside your home. For that, use a VPN (Tailscale, WireGuard) or a real reverse proxy with TLS. **Don't port-forward 8080 to the open internet.**

**Step 1: no auth setup needed.** As of PR-A2 auth is always on. On first boot the UI walks you through creating a root admin account; subsequent boots show the login screen. Skip directly to Step 2.

The first time you visit the UI after enabling, it walks you through creating an admin account. Make the password strong: 12 characters minimum, a memorable passphrase is fine.

**Step 2: expose the frontend port to your LAN.** In `docker-compose.yml`, find the `frontend` service's `ports:` block and remove the `127.0.0.1:` prefix:

```yaml
frontend:
  ports:
    - "8080:80"                # was "127.0.0.1:8080:80"
```

Leave the **backend** port alone, keep it as `"127.0.0.1:8000:8000"`. The frontend container talks to the backend over Docker's internal bridge network (service name `backend:8000`), so the host's published backend port is only used for direct local debugging. Keeping it loopback-only means the backend stays unreachable from the LAN.

Recreate:

```bash
docker compose up -d
```

`docker compose ps` should now show the frontend's port mapping as `0.0.0.0:8080->80/tcp` (rather than `127.0.0.1:8080->80/tcp`). If it doesn't, your compose file change didn't take. Try `docker compose down && docker compose up -d`.

**Step 3: connect from the other device.** Find your Docker host's LAN IP:

| Host OS | Command | What to look for |
|---|---|---|
| Windows | `ipconfig` in PowerShell | `IPv4 Address` under the active Wi-Fi or Ethernet adapter |
| Linux   | `ip addr` | `inet 192.168.x.x` under the active interface |
| macOS   | `ifconfig` or System Settings -> Network | The IP under the active adapter |

Visit `http://<that-IP>:8080` in the device's browser. You should see the PlexBackUp login screen. Sign in with the admin you created in Step 1.

**If it doesn't connect:**

* **Firewall.** Windows Defender Firewall almost always prompts the first time Docker Desktop tries to listen on a non-loopback interface. If you missed the prompt, the device's browser will time out. Open `wf.msc` and either temporarily disable the Public profile firewall to confirm the cause, or (better) add a permanent inbound rule allowing TCP 8080 on the private profile only.
* **Subnet isolation.** Some routers separate the main Wi-Fi from a guest or IoT network. Both devices need to be on the same subnet for direct IP access to work.
* **Auth gives 401 forever.** After enabling auth for the first time, if the UI keeps bouncing you back to the login screen, it's almost always one of: the WebSocket close code 4001 path firing because the persisted token is stale (clear browser session storage and log in again), or your time-of-day clock skew is several hours off the host's (rare, but JWT `exp` validation is sensitive to this).
* **Backend wasn't restarted.** Env var changes only take effect on container recreate. `curl http://localhost:8000/api/auth/status` from the host should report `{"auth_enabled": true, ...}`. If it still says `false`, run `docker compose down && docker compose up -d`.

**Reverting:** to lock the UI back to localhost only, change the line back to `"127.0.0.1:8080:80"` and `docker compose up -d`.

---

## Migration from v0.8.0

On first boot of v0.9.0, the FastAPI server checks for legacy `plex_url`/`plex_token` fields in `server_data/settings.json`. If both are non-empty AND the registry is empty, it registers them as a server named `"Default"` and clears the legacy fields. Your existing setup keeps working without any manual reconfiguration. You can rename the migrated server in one click from the Servers tab.

If you already had servers registered and the legacy fields are still set (shouldn't happen, but a defensive check), the legacy fields are quietly cleared with no duplicate row.

**Schedules.** Schedules created in v0.8.0 don't carry a `source_server_name`. The scheduler skips such schedules with one warning per fire and rolls their `next_run_at` forward. Open the **Schedules** tab and edit each one to pick a registered server.

---

## Where to read the code

| Concern | Module |
|---|---|
| Shared session, retry policy, Plex auth | `services/auth.py` |
| Per-run state and `ContextVar`s | `services/state.py` |
| The four-tier matcher | `services/resolver.py` |
| Snapshot pipeline (`run_snapshot()`) | `services/snapshotter.py` |
| Restore pipeline (`run_restore()`, Merge and Replace) | `services/restorer.py` |
| Dashboard, `submit_with_context()` helper | `services/dashboard.py` |
| Operator-tunable knobs | `services/tunables.py` |
| Job queue and worker | `server/jobs.py` |
| FastAPI routes | `server/app.py` |
| 4 Hz WebSocket broadcaster | `server/ws.py` |
| Multi-destination fan-out | `server/fan_out.py` |
| App auth (login, JWT, refresh) | `server/auth_db.py`, `server/auth_router.py` |
| At-rest encryption (Fernet) | `server/secrets.py` |
| Log scrubber (credential filter) | `server/log_scrubber.py` |
| PIN preflight | `server/preflight.py` |
| Throttled managed-user token capture | `server/user_capture.py` |
| `media.db` schema and DML | `server/media_db.py` |
| Snapshot `.db` writer | `server/snapshot_capture.py` |
| Snapshot registry (`snapshots.db`) | `server/snapshot_registry.py` |
| Snapshot serializer (JSON sidecars) | `server/snapshot_serializer.py` |
