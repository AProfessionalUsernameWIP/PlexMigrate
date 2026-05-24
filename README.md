# Hestia-MediaManager
**Version 0.18.0** (snapshot schema v18)
A tool that backs up and moves your Plex watch history, listening history, playlists, collections, and star ratings between Plex servers, without losing any data.

> **Three docs, three audiences. Pick the one that fits you:**
>
> * **[QUICKSTART.md](QUICKSTART.md)** - the 5-minute setup. Read this if you just want to get Hestia-MediaManager running and take a snapshot.
> * **README.md** (this file) - the **operator manual**. How to install, configure, run, schedule, troubleshoot, and tune Hestia-MediaManager. Written for anyone who runs the tool, no engineering background assumed.
> * **[OVERVIEW.md](OVERVIEW.md)** - the **technical architecture**. How the engine, databases, API surface, and concurrency model are wired together, and why each decision was made the way it was. Also covers advanced setup topics like LAN exposure and the security architecture. Written for contributors and the technically curious.

> **A note on quality and bug reports.** Hestia-MediaManager uses a **CI/CD pipeline** that runs **unit tests before any commit lands in the repo**. The pipeline will be made available to end users so you can see what gets checked on each change. This doesn't catch every bug, but it does mean issues are much more likely to be caught before they ship.
>
> If you do find a bug, **please report it**. I will make every effort to fold any reported failure case into the test suite as a new unit test, so the same issue can't sneak back in. Please be patient with us and send the bug reports through - they make the tool better for everyone.

Hestia-MediaManager runs as a Docker + Web UI app. `docker compose up --build` (or `make docker`) brings up a FastAPI backend and a React frontend in two containers. The web dashboard shows live job state, every per-run option has a labelled form control, and you can save scheduled recurring exports. Jump to [Docker and Web UI](#docker-and-web-ui).

**Recent: snapshot rename and capture pipeline.**
The "export to `.plexexport.json` file" model is now three layers:
`media.db` (the live working store), per-server snapshot `.db` files
(immutable point-in-time copies), and `snapshots.db` (the registry
indexing the snapshot files). The Exports panel reads the registry,
grouping snapshots by server with per-server retention enforcement and
a `db_admin`-gated "Clear all snapshots for this server" button.
Settings has a new Snapshot Retention block (global ceiling plus
per-server overrides; the global wins when an override is higher).
The on-disk directory renamed from `plex_exports/` to `snapshots/`,
with an auto-migration that runs once on startup. Pre-rename
`.plexexport.json` files relocate to `snapshots/legacy/` and stay
readable via a compat shim.

---

## Why Hestia-MediaManager Exists

Plex is where a lot of people put a real chunk of their lives. Years of watch history. Ratings you actually thought about. Playlists you built one track at a time. The "watched" badges that mean you can see at a glance what you've already finished. None of that is media. All of it is yours.

The problem: Plex (and Emby, and Jellyfin) don't ship a real backup story for that data. You can re-buy disks, re-rip media, re-index a library, but you can't trivially recover the human metadata that turns a folder of files into "your" library. If the Plex database goes sideways, that data is gone. If you migrate to a new box, you start over. The standard line is "Plex stores it for you", and that's fine right up until it isn't.

Hestia-MediaManager fills that gap. It captures every operator-relevant piece of metadata, including watch history, ratings, playlists, collections, and per-user state, into a portable snapshot file. It restores that snapshot back into the same server or a different one, and it can transfer server to server directly with no intermediate file on the happy path. You can run it on a schedule, fan a single capture out to multiple destination servers, or use it as a one-time migration tool when you replace hardware.

The short version: **Plex doesn't have a backup button. Hestia-MediaManager is that button.**

A few things worth knowing up front:

* **It runs alongside Plex, not against it.** Hestia-MediaManager talks to Plex through the same API your phone app uses. Active streams keep playing. Nothing on the media disk is touched. You can run a snapshot while the family is watching TV and nobody will notice.
* **It never deletes anything by default.** Restore Merge mode (the default) is strictly additive: it only adds what is missing. There is also a destructive Replace mode for "I want this destination to be an exact mirror of a known capture", and it requires you to type a confirmation word before it will run.
* **It is built for more than just Plex.** The data model is deliberately backend-agnostic at rest. Adding Emby or Jellyfin support is on the roadmap and does not require a rewrite, just new "gather" and "restore" code for those platforms. The full reasoning is in [OVERVIEW.md](OVERVIEW.md).

---

## What This Does

Hestia-MediaManager works through Plex's built-in API, the same interface your Plex app uses when you hit play, mark something watched, or build a playlist. It doesn't touch your media files, move any data on disk, or require you to stop using Plex while it runs. You can keep watching TV or listening to music on any device while a snapshot or restore runs in the background.

There are two steps.

**Snapshot.** Run this on your old server, or before you rebuild. Hestia-MediaManager connects to Plex, reads your watch history, resume positions, star ratings, playlists, and collections, and saves them into a per-server snapshot `.db` file (one row in `snapshots.db` per capture). Plex must be running on that machine for this step.

**Restore.** Run this on your new or freshly rebuilt server. Hestia-MediaManager reads the snapshot and restores everything it can find, matching each item using four methods in order: by its global ID (IMDb, TMDB, TVDB, or MusicBrainz), by its exact file path, by a path-suffix match (for cross-platform migrations, see below), and finally by title. Plex must be running on the target machine for this step, but nothing else needs to stop. Active streams and in-progress playback are not affected.

Between those two steps, the snapshot is just a file on disk. Copy it however you like (USB drive, network share, cloud storage) and run the restore whenever you're ready.

Hestia-MediaManager logs everything it does and produces a plain-English troubleshooting report for anything it couldn't restore automatically.

---

## Data Safety

Hestia-MediaManager has two restore modes. The defaults are conservative; the destructive one requires you to type a confirmation word before it will run.

### Merge mode (the default, additive only)

**Hestia-MediaManager never deletes, overwrites, or reduces any data on your target server in Merge mode.** Every restore is strictly additive. It only adds what is missing.

Here is what "additive" means for each data type:

- **Watch history**: If an item on the new server already has a higher view count than the snapshot, the script leaves it alone. It only adds views when the snapshot count is strictly higher. Resume positions (where you paused) are only restored if the new server has no saved position for that item.
- **Playlists**: If a playlist with the same name already exists, the script adds any items that are missing from it. Items already present are skipped. The playlist is never deleted or replaced. Descriptions are never overwritten. Item order from the original playlist is preserved.
- **Collections**: Same as playlists. Existing collections get missing members added, and nothing is removed.
- **Ratings**: If an item on the new server already has a star rating, the script skips it. Your rating on the new server always wins.

You can safely run a Merge restore multiple times on the same server. It won't create duplicates or reduce your data.

### Replace mode (opt-in, destructive)

Replace mode makes the destination match the snapshot exactly. It re-scrobbles watch counts to the snapshot value (so it can lower them, not just raise them), overwrites ratings, and recreates playlists and collections from scratch. The UI requires you to type the word `REPLACE` before it will submit the job, and a pre-replace safety-belt snapshot is captured into `snapshots.db` automatically so you can roll back if you don't like the result. If the safety belt fails to capture, the destructive write does not fire.

Use Merge for "I'm migrating to a new server" or "I want to keep both sides going". Use Replace for "I want this destination to be an exact mirror of a known-good capture", and only after you've practised once on a non-critical library.

---

## Where Hestia-MediaManager keeps its state

Hestia-MediaManager uses three small SQLite files under `server_data/`. You usually don't need to touch them directly, but knowing what each one stores makes backups and troubleshooting much easier:

| File | What it stores |
|---|---|
| `auth.db` | App user accounts, password hashes, and JWT refresh tokens. Nothing about Plex itself. |
| `media.db` | The live working pool of every metadata item, watch event, rating, playlist, and collection Hestia-MediaManager has ever ingested. |
| `snapshots.db` | An index of every captured snapshot `.db` file on disk, with per-server retention metadata. |

Each one can fail or be rebuilt independently of the others, which is why the tool is safe to leave running for months. The architectural reasoning (WAL mode, single-writer pattern, GUID keying) lives in [OVERVIEW.md: Databases](OVERVIEW.md#databases).

---

## Docker and Web UI

Hestia-MediaManager runs as a web layer over the engine: a FastAPI backend under `server/` and a React UI under `frontend/`, both wrapping the shared engine code under `services/`.

### Requirements

* **Docker Desktop** (macOS / Windows) or **Docker Engine + Docker Compose v2** (Linux). Nothing else; you do *not* need to install Python, Node, or npm on the host. All dependencies live inside the containers.
* Your Plex Media Server should be reachable from the backend container. The default is `http://host.docker.internal:32400` (Plex on the same machine). `host.docker.internal` is mapped to the host gateway in `docker-compose.yml`, so this works on Linux too. If Plex lives elsewhere, change the URL under **Settings** in the web UI.

### One-command startup

From the project root:

```
docker compose up --build
```

Or, equivalently, using the bundled Makefile:

```
make docker
```

This builds two images (backend Python and frontend nginx) and brings them up on the same compose network. The first build takes a couple of minutes. Later builds use the layer cache and are fast.

Once both containers are healthy, open <http://localhost:8080> in your browser. Open the **Settings** tab and paste your Plex server URL and authentication token. Settings persist on a host bind mount (`./server_data/settings.json`).

### What the web UI gives you

* **Dashboard tab**: the live view of the current run. The header shows the libraries queued for this run, which library is being processed right now, and (when a direct transfer is scoped to specific users) which user the engine is on. Below that you get the thread pool counts, run stats, match resolution stats, per-library progress bars with ETA, the colour-coded activity feed, and a Network Activity panel that charts HTTP status codes, requests-per-second, and average latency over the last 60 seconds. Elapsed and ETA freeze at the final values when a job ends so you can see what the actual run duration was. Updates over a WebSocket at 4 Hz.
* **Run Job tab**: every per-run option has a clearly labelled form control. Pick snapshot, restore, or direct server-to-server transfer; select libraries (or snapshot rows), set worker count, toggle verbose and strict match, fill in path remap if needed, then submit. Direct mode adds a Users section with checkboxes for the owner and every managed user that exists on both servers - uncheck anyone you don't want to migrate. Jobs run one at a time; subsequent submissions queue.
* **Servers tab**: register, edit, test, and remove Plex servers by friendly name. The Server Users block under each row lists the owner and every Plex Home managed user; click the owner's display name to edit it inline (the chosen name propagates to the dashboard header, the activity feed, and the direct-transfer user selector). Removing a server is a cascade - schedules referencing it, snapshot files produced by it, and per-run log directories under its slug all get deleted with a confirmation dialog showing the counts.
* **Schedules tab**: create, edit, enable / disable, and delete recurring snapshot schedules. Schedules fire in the container's configured timezone (set the `TZ` env var in `docker-compose.yml`), and the topbar shows a live server-time clock so you always know what time the schedule engine sees. Frequency: hourly, daily, or weekly, at a wall-clock time you choose.
* **Logs tab**: a three-pane browser over `plex_logs/`. Click a run directory, click a file, read the contents in the browser. The viewer has a case-insensitive keyword filter - type any substring and matching lines stay visible with the match highlighted, everything else hides. Files larger than 4 MB show the tail.
* **Exports tab** (under Settings): the snapshot registry, grouped
  by server. Each row is one captured snapshot with its `.db` size,
  library count, user count, and capture timestamp. Download
  streams the snapshot as `.plexexport.json`, generated on demand
  from the `.db` unless a pre-built sidecar exists. Each per-server
  group has a "Clear all snapshots for this server" danger button
  (db_admin gated). A separate "Legacy JSON archives" section lists
  any pre-PR-13 `.plexexport.json` files relocated to
  `snapshots/legacy/` on first boot.
* **Settings tab**: Plex URL, Plex token (write-only, never echoed back to the browser), and the default values for every per-run option. Output and log paths must be container-visible - Windows host paths like `Y:\plexexports` are rejected with a message explaining how to bind-mount external drives in `docker-compose.yml`.

### Stop / restart / inspect

| Action | Command |
|---|---|
| Stop both containers, keep volumes | `docker compose down` |
| Stop containers and wipe volumes (does NOT touch host bind mounts) | `docker compose down -v` |
| Rebuild after a code change | `docker compose up --build` |
| Tail backend logs | `docker compose logs -f backend` |

### Where data lives

Three host directories are bind-mounted into the backend container so all data outlives the container lifecycle:

| Host path | Container path | Contents |
|---|---|---|
| `./snapshots/` | `/app/snapshots/` | Per-server snapshot `.db` files. `snapshots/legacy/` holds any pre-PR-13 `.plexexport.json` archives moved there by the first-boot migration. |
| `./plex_logs/` | `/app/plex_logs/` | Per-run log directories |
| `./server_data/` | `/app/server_data/` | `settings.json`, `schedules.json`, `servers.json`, `media.db`, `snapshots.db` (snapshot registry, PR-13), `auth.db` (PR-A1), and the binary `.keyfile` used for at-rest encryption (v0.9.5+) |

You can inspect and edit everything in the table from the host. The JSON files use 2-space indent and are easy to diff. The `.keyfile` is 32 raw bytes - don't open it in a text editor, don't commit it to source control (`.gitignore` already excludes it), and don't delete it unless you're prepared to re-enter every registered server's token.

### Security and advanced setup

The basics: **auth is always on**. On first boot the UI walks you through creating a root admin account; every subsequent boot shows the login screen. Every API call and the WebSocket require a login. Plex tokens are encrypted at rest. You don't need to configure any of this; it just works.

If you want to dig deeper, the following topics live in [OVERVIEW.md](OVERVIEW.md) rather than here, because none of them are required to get Hestia-MediaManager running:

* **Security architecture** - at-rest encryption (Fernet keyfile), JWT auth, log scrubber, file permissions, and Windows-vs-Unix ACL caveats.
* **Exposing Hestia-MediaManager to other devices on your network** - the optional walkthrough for accessing the web UI from a phone, tablet, or other desktop on your home LAN, including firewall and subnet gotchas.

---

## Makefile

Convenience targets at the project root:

| Target | What it does |
|---|---|
| `make docker` | Runs `docker compose up --build`. Builds and starts the full web stack (backend + frontend) on `http://localhost:8080`. |
| `make docker-rebuild` | Forces a no-cache backend rebuild then starts everything. Use after a Python source change when you want to be certain the container picked up the new code. |
| `make clean` | Removes `./venv/` if one exists. Does not touch `snapshots/`, `plex_logs/`, or `server_data/`. |
| `make help` (or just `make`) | Prints the target list. |

---

## Multi-Server Support

Starting in v0.9.0, Hestia-MediaManager manages a registry of Plex servers rather than a single connection. Every snapshot, restore, and schedule targets a specific registered server by friendly name. A new direct transfer mode moves data from one registered server straight into another in memory.

### Registering servers

Open the **Servers** tab in the web frontend. Click **+ Add Server**, fill in:

* **Friendly name:** any string. You'll pick this in the Run Job form and schedules. Names must be unique.
* **Server URL:** full URL including protocol and port (for example `http://host.docker.internal:32400`).
* **Plex authentication token:** same token you'd find via the Plex web UI's `X-Plex-Token` URL param.

When you save, Hestia-MediaManager adds the server to the registry and immediately probes the connection. The probe populates the status indicator and discovers the library catalogue. You can later **Test** the connection, **Edit** the fields, or **Remove** the server from the registry.

> **Removing a server from the registry never deletes any snapshot files or log directories produced from that server.** The registry is just a pointer table. The files on disk live independently.

### Targeting registered servers

The **Run Job** tab has an operation selector (Snapshot, Restore, or Direct transfer) and a server selector below it. In direct transfer mode the form shows a side-by-side "Source server -> Destination server" picker so the direction of data flow is unambiguous. Library and snapshot pickers populate from the selected server.

### Live status indicators (v0.9.1)

The Servers tab and the Run Job server selectors poll each registered Plex server every 30 seconds with a lightweight `/identity` request. The result is a coloured dot next to each server (green for reachable, red for unreachable or auth failure, amber for unknown) and the current response time in milliseconds. The poll is cheap. It doesn't enumerate libraries or fetch metadata, so leaving the web UI open in the background won't generate meaningful API load on your Plex servers.

The Servers tab also has a per-row **Refresh** button that runs the heavier `test_connection` probe and re-enumerates the libraries. The **Add Server** form has its own **Test Connection** button that probes the URL and token before the row can be saved. The Remove button asks for confirmation and reminds you that removing a server doesn't delete any snapshot files or log directories on disk.

### Direct transfer fallback (v0.9.1)

If the in-memory direct path fails for a library mid-transfer (network blip, unexpected response, very large library), Hestia-MediaManager automatically falls back to a temporary snapshot-and-restore for that library only. The other libraries keep going on the direct path. The dashboard announces the fallback in the activity feed, and the end result for your data is the same either way. The mechanics of how the fallback file is written, restored, and cleaned up live in [OVERVIEW.md: Direct transfer](OVERVIEW.md#direct-transfer).

### Per-user transfer scope (v0.9.6+)

When you pick **Direct Transfer** in the Run Job form, after both servers are chosen a new **Users** section appears. It shows three groups computed live from each server's `/accounts` data: users present on both servers (with checkboxes, default-checked), users present only on the source (greyed out, "Not on destination server"), and an informational footer explaining how to invite missing users. The owner appears in the list alongside managed users. Uncheck them and the run skips the entire library-level data block (watch history, playlists, library-level collections, ratings) with a clear log line. Personal collections (Plex Pass feature) ride with each included user's block automatically; pre-v0.9.7 these were silently dropped from every snapshot, now they're correctly captured and restored per-user.

### Fan-out transfer (v0.10.0)

In the Run Job form's **Destination Server** picker, you can now pick more than one server. As soon as a second one is checked the panel re-labels itself **Destination Servers (Fan-out)** and a small banner notes how many destinations the job will write to. Submit, and the dashboard auto-switches to the fan-out view: a top strip showing the source and per-state counts, and one card below per destination, each with its own status badge, log directory, and progress bars.

This works for both **Direct Transfer** (one source server feeding many destinations) and **Restore** (one set of snapshot files restored into many destinations). The same additive-only merge rules apply per destination. Nothing on any destination is ever deleted or reduced. Destinations run in **parallel** on their own threads; a destination that fails takes only its own card down, and siblings keep running. Per-user filtering, library selection, and remap-path are applied to every destination in the job.

For users on the per-user filter: the included intersection is computed across the source AND every destination, so a managed user must exist on all of them to ride along by default. You can still uncheck individuals to exclude them entirely.

Each destination has its own per-library `_success` / `_fail` / `troubleshoot.log` files, so you can read each destination's results independently. The shared run-level streams (`runtime.log`, `errors.log`, `media.log`) currently aggregate across destinations of one fan-out job; a future release will split them per destination. The technical detail of how the parallel destinations stay isolated lives in [OVERVIEW.md: Fan-out coordination](OVERVIEW.md#fan-out-coordination).

### Filename and log directory conventions

Log directories and snapshot filenames from v0.9.0 onwards carry the friendly server's slugified name as a prefix, so outputs from different servers never collide:

| Operation | Old (v0.8.0) | New (v0.9.0) |
|---|---|---|
| Snapshot file | `Movies_20260510_135425.plexexport.json` | `Movies_Plex1_20260510_135425.plexexport.json` |
| Log directory | `run_20260510_135425_PASS/` | `run_Plex1_20260510_135425_PASS/` |
| Direct transfer log dir | (didn't exist) | `run_Plex1-to-Plex2_20260510_135425_PASS/` |

> **Upgrading from v0.8.0?** On first boot, your old single-server settings are migrated into the registry as a server named `Default` automatically. Schedules created in v0.8.0 need to be edited once to pick a registered server. The full migration mechanics are in [OVERVIEW.md: Migration from v0.8.0](OVERVIEW.md#migration-from-v080).

---

## Smart Playlists

Smart playlists are playlists whose contents are generated by a saved filter (for example, "all unwatched Action movies added this year"). Hestia-MediaManager **cannot transfer smart playlists automatically** because the filter query contains server-specific IDs that are different on every Plex installation.

When Hestia-MediaManager encounters a smart playlist, it:
1. Records it in the failure log with the category "Smart Playlist - Requires Manual Recreation."
2. Saves the original filter URL in the run log so you have it for reference.
3. Does not create any placeholder playlist on the target server.

To restore a smart playlist: open Plex on the target server, create a new Smart Playlist, and re-enter the same filter criteria. The run log entry for that playlist shows the original filter URL.

---

## Plex Home Users

If your Plex server is linked to a Plex.tv account and you use Plex Home (multiple user profiles sharing one server), Hestia-MediaManager automatically snapshots and restores each managed user's watch history, playlists, and ratings independently. Each user's data lives in the snapshot under a `"users"` section and is restored into the correct profile on the target server.

**Requirements for multi-user support:**
- The server must be linked to a Plex.tv account (not using a LocalAdminToken).
- The managed users must exist on the target server with the same usernames before you run the restore.

**What happens if a user is on the old server but not the new one yet?** Hestia-MediaManager logs which users it found on the target server and which ones exist in the snapshot before it starts restoring, so you can see the gap immediately. Users not found on the target are skipped with an INFO log, not an error. The end-of-restore summary lists which users were restored and which were skipped, by name. Re-invite the skipped users to the new server and re-run the restore to restore their data.

If the server is not linked to Plex.tv, Hestia-MediaManager logs a note and continues. Only admin account data is processed, with no error.

### Managed user PINs and the preflight warning

If a managed user has a Plex Home PIN set and Hestia-MediaManager doesn't have a captured token for them, the Run Job form shows a **preflight warning** listing the at-risk users before the job submits. You can either fix it (capture the PIN in the Servers tab, then re-run) or continue, in which case those users are dropped from the run with a clear log line.

The technical detail of how managed user auth actually works (parallel token-based auth with PIN fallback, background token capture, why we don't silently fall back to admin impersonation) lives in [OVERVIEW.md: Managed user authentication](OVERVIEW.md#managed-user-authentication).

### How users are identified across servers (the app_user_uuid)

Hestia-MediaManager tracks three identifiers per user, each with a different lifetime and scope:

| Identifier | What it is | Mutable? |
|---|---|---|
| `user_handle` | Backend username (Plex username, Jellyfin Name, Emby Name) | Yes — when the operator renames on the backend |
| `backend_user_id` | Backend-assigned per-server stable id (Plex.tv numeric userID; Jellyfin / Emby GUID) | Usually stable; can rotate on some backends |
| `app_user_uuid` | App-generated canonical anchor — the validation handle this app keys off | **No** — immutable for the lifetime of the row |

**The invariant: every user added to this app gets an `app_user_uuid` generated at insert time.** That covers every path a user can enter the app — managed-user sync, snapshot capture, snapshot restore, the User Management endpoints, the Plex Home per-user-token save endpoint, inline cross-platform user creation. The two writer helpers (`upsert_managed_user` + `get_or_create_server_user`) generate via `generate_unique_app_user_uuid` and the column has a partial UNIQUE index so duplicates fail loudly rather than silently land. Legacy rows from before the v12 migration are filled on the next app boot via an idempotent backfill. There is no path that adds a user without a UUID.

**Format:** `<Service>-<HostNameSlug>-<server_uid>-<userkey>` — for example `Plex-JadeTV-plex_a1b2c3d4e5f67890abcdef1234567890-a3f9c2d8`. The Service segment is "Plex" / "Jellyfin" / "Emby"; HostNameSlug is the cosmetic per-server label (auto-refreshed on server rename so the slug stays human-readable); `server_uid` is the prefixed server identifier (also immutable); and the 8-hex `userkey` is randomly generated per (server, user) pair. What stays the same across a server rename: everything except the HostNameSlug. What stays the same across the user's lifetime: everything — `userkey` is generated once and never changes.

**Why an app-generated identifier rather than reusing the backend's own user id?** Each backend assigns its own user ids in its own ID space; the ids don't cross between backends and the app doesn't control them. `app_user_uuid` is the application's own anchor: format we choose, lifetime we control, present on every row regardless of backend, and useable as a primary key in cross-server identity links.

**Cross-server identity:** the `user_identity_map` table keys off two `app_user_uuid` values rather than (server_id, user_handle) tuples. That means an operator-authored mapping like "the Plex 'Crystal Jean' on Server A is the same human as the Jellyfin 'crystal.jean' on Server B" survives renames on either side, `backend_user_id` rotation, and even one of the backends being re-registered. The auto-link helper additionally writes "auto_copy" rows for same-(service_type, backend_user_id) pairs so the operator doesn't have to manually map their own Plex.tv account across two of their own Plex servers.

**Where the resolution happens:** snapshot restore, direct transfer, and playlist copy all walk the same 5-step chain when picking the destination user for each source user's payload:

1. Per-job operator override (Map decision from the cross-platform preflight modal)
2. `user_identity_map` lookup (authoritative)
3. `backend_user_id` direct match within the same service_type
4. Case-insensitive username match (the legacy fallback)
5. Owner-role single-admin fallback (when the source user is the owner and the destination has exactly one admin)

The `strict_identity_resolution` tunable cuts the chain short after step 2 so operators who want every routing to come from an explicit map (or operator-confirmed preflight resolution) can lock that in. The full primer also lives under **Help > Topics > Servers > How users are identified across servers** in the web UI.

---

## Step-by-Step: Migrating to a New Server

1. **On your old server:** Run the snapshot from the Run Job tab in the web UI. Select the libraries you want to back up.
2. The script creates a per-server snapshot `.db` and (optionally) `.plexexport.json` sidecars in the `./snapshots/` folder, one per library.
3. **Copy those files** to the machine where your new server runs. USB drive, network share, cloud storage; any method works.
4. **On your new server:** Make sure your media files are accessible and Plex has scanned them. The items must appear in Plex before you can restore.
5. Run the restore, pointing at the snapshot files.
6. Check the `./plex_logs/` folder for a summary and any items that need manual attention.

---

## Log Files

All logs land in `./plex_logs/` (or the path you set in Settings). Filenames carry a timestamp so runs never overwrite each other.

| Log file | When created | What's in it |
|---|---|---|
| `run_YYYYMMDD_HHMMSS.log` | Always, one per run | Full transcript: startup, library discovery, every action, all successes and failures, final summary. Start here when something goes wrong. Add `--verbose` for DEBUG detail. |
| `{LibraryName}_success_YYYYMMDD_HHMMSS.log` | At least one item in that library succeeded | Every successful item, the matching method (GUID lookup, file path, or title search), and the action taken. Action tags: `[CREATED]`, `[APPENDED]`, `[RATING SET]`, `[SKIPPED - ...]`. Ends with a totals summary and success rate. |
| `{LibraryName}_fail_YYYYMMDD_HHMMSS.log` | At least one item in that library failed | Every failed item, the GUID and file path tried, and the specific reason. Same summary block as the success log. |
| `troubleshoot_YYYYMMDD_HHMMSS.log` | Any failures occurred | Failures grouped by category (file not found, ambiguous title match, etc.) with a plain-English explanation and step-by-step fix for each, plus a "Next Steps" section. |
| `unresolved_YYYYMMDD_HHMMSS.log` | Items failed all matching tiers | One-line-per-item checklist for manual restoration in Plex, with a short intro explaining what to do with the file. |

---

## Common Problems

If you hit an issue not covered by this README, please file it on the issue tracker and include the relevant log snippet from `plex_logs/`. The most common failure modes are: `externally-managed-environment` pip errors, missing modules, dashboard refresh quirks on Linux, the "local:// GUID" music-track edge case, the empty-`guids` IndexError in Play Count restore, and the playlist 400 bad_request on large static playlists. Each one usually has an obvious cause in the logs.

If you hit something that isn't documented in either file, the run log directory under `plex_logs/run_<slug>_<timestamp>_FAIL/` carries the per-library success/fail logs, the runtime/errors/media streams, and a generated `troubleshoot.log` keyed by failure category. That's the first place to look before reporting anything.

---

## Tips for Large Libraries

- Set the worker count to 16 or higher on machines with many CPU cores to speed up processing.
- Run the snapshot overnight if your library is very large. Hestia-MediaManager is safe to leave running.
- After restore, check the Plex dashboard to verify watch history appears correctly on a few items before assuming everything is done.
- You can safely run the restore more than once. The additive merge logic means repeated runs only add what's still missing. They won't create duplicates.
- The web Dashboard tab shows a live view of every run: thread pool counts, per-library progress bars with ETA, run stats (completed / skipped / failed / unresolved), a match resolution breakdown (GUID / filepath / suffix / fuzzy), and a colour-coded activity feed.

### Collection performance settings (v0.12.3)

Collections are one of the heaviest parts of a snapshot or direct transfer when you have many home users. On a server with 12 users and 300 library-wide collections, the naive approach sends 3,600 API calls just for collections, one `coll.items()` round-trip per collection per user, even though all 300 are visible to everyone. Two settings in the **Engine** section of the Run Job form address this directly.

#### Why collections are expensive per-user

Plex doesn't offer a "give me only this user's personal collections" endpoint. The only way to find a user's personal collections (ones they created themselves that aren't visible to everyone) is to fetch the full collection list from their account, then subtract the library-wide ones. That subtraction is the right behaviour. The problem is that the pre-v0.12.3 code paid the full serialization and items-fetch cost on every collection in the list before the subtraction, so library-wide collections were processed N times (once per user).

Three-layer optimisation now applies automatically on the per-user pass:

1. **Early exit.** If every collection the user can see is already in the library-wide set, the user has no personal collections at all. Hestia-MediaManager returns immediately with no further API calls.
2. **Skip-before-work.** For the remaining users who do have personal collections, library-wide entries are skipped before `coll.items()`, serialization, log writes, or dashboard counter increments fire. Only genuinely personal collections pay the full cost.
3. **Fast owner detection** (opt-in). See the table below.

| Setting | What it does | When to use it | When to leave it off |
|---|---|---|---|
| **Skip collections** | Omits the collection gather entirely, owner block and all per-user passes. Watch history, playlists, and ratings transfer normally. | You don't need collections at the destination, or the destination server will build its own (e.g. a fresh install that auto-generates franchise collections). Fastest possible transfer. | You have personal Plex Pass collections you want to preserve across servers. |
| **Skip playlists** | Omits the playlist gather entirely, owner block and all per-user passes. Watch history, collections, and ratings transfer normally. | Migrating to a fresh server and you'd rather rebuild playlists by hand, or the bulk of your playlists are smart playlists (which can't transfer automatically and would all appear in the failure log anyway). Also useful when a quick watch-history sync is all you need. | You have regular (non-smart) playlists you want preserved at the destination. |
| **Fast collection detection** | Reads Plex's `librarySectionUserID` attribute on each collection to determine ownership without a set lookup. `None`/`0` = library-wide (skip); any other value = personal (process). Eliminates even the set-membership check for each item. | You are on **Plex Media Server >= 1.32** and have a large number of library-wide collections (roughly 100+) and/or many home users (5+). The gains are most visible when early-exit fires for most users but the remaining users still have a large list to iterate. | You are on an older Plex build. The engine falls back to the standard rating-key method automatically if the attribute isn't there, so it is safe to enable, but you gain nothing on old servers. |

**Rule of thumb for large libraries:**
- If you have fewer than 5 home users and/or fewer than 50 collections, the built-in three-layer optimisation is already fast enough. No settings change needed.
- If you have 10+ home users **and** 200+ library-wide collections, enable **Fast collection detection**.
- If you're doing a speed-first migration and will rebuild collections by hand afterward, enable **Skip collections**.
- If most of your playlists are smart playlists, enable **Skip playlists**. They'll all fail anyway and skipping them is honest and faster.
- Never enable both skip options and fast detection together; if you're skipping collections the fast-detection setting has nothing to act on.

---

## Understanding Performance and Threading

All worker threads run on the host (the machine running the container), **not** on your Plex server. Speed depends on your network to Plex, how fast Plex answers, and how many parallel requests Plex tolerates. The default thread count comes from the host's CPU core count, but Hestia-MediaManager spends almost all its time waiting on Plex, so the host's core count barely matters.

> **A note for the technically curious.** If you've heard that Python's "Global Interpreter Lock" (GIL) prevents Python from using more than one CPU core, that's true, and almost completely irrelevant for this tool. Hestia-MediaManager spends roughly 99% of its wall-clock time waiting for Plex HTTP responses or for SQLite to fsync, and both of those waits release the GIL. A 16-worker pool against Plex really is 16x parallel for the part that matters. The reasoning is fleshed out in [OVERVIEW.md: Concurrency, Python, and the GIL](OVERVIEW.md#concurrency-python-and-the-gil).

### If it's running slowly

1. **Lower the worker count** in the Worker threads field (Run Job tab).
2. **Get on the same LAN as Plex.** Single-digit milliseconds on a LAN, hundreds over the internet.
3. **Check whether Plex is busy** streaming or transcoding. That contention shows up as slow API responses.

### If it's running well and you want it faster

Increase the worker count, but watch the **Failed** count in the dashboard. Failed items mean Plex didn't answer in time or returned an error. If Failed climbs, Plex is overwhelmed; drop workers back down.

### Direct server-to-server transfer

Both servers receive API calls at once, so cut workers further to keep both responsive. The Run Job tab shows a banner reminding you of this when you pick direct transfer mode.

### Recommended starting points

| Scenario | Recommended worker count | Notes |
|---|---|---|
| Host and Plex server on the same LAN | The default (`min(32, cpu_count x 4)`) | Network is fast and reliable. Plex itself is the bottleneck, and most home servers handle this comfortably. |
| Host remote from Plex / over the internet | **Start with 8** and increase from there | High network round-trip time means more parallel requests in flight, not all of which can be served before timing out. Lower workers = fewer timeouts. |
| Direct server-to-server transfer | **Half the default** (`min(16, cpu_count x 2)`) and monitor both servers | Both servers feel the load. The lower starting point gives you headroom to increase if both stay healthy. |

### Registering many servers

Every snapshot adds load to whichever server it reads from. Stagger your scheduled windows so two schedules don't fire at the same minute against the same Plex. Large music libraries hit the API hardest, so give those breathing room. The Servers tab shows a banner reminding you of this when you have multiple servers registered.

---

## Architecture, design tradeoffs, and roadmap

The architectural deep dive lives in **[OVERVIEW.md](OVERVIEW.md)**. It covers:

* The snapshot lifecycle from `POST /api/job/snapshot` through to a written snapshot file, step by step.
* Restore Merge vs Replace semantics and the safety-belt snapshot pattern.
* How direct transfer keeps the snapshot in memory and falls back to a chained path per library if anything goes wrong.
* Fan-out coordination across multiple destinations using `ContextVars`.
* The 4 Hz WebSocket data flow that powers the live dashboard.
* The three SQLite databases (`auth.db`, `media.db`, `snapshots.db`) in WAL mode with single-writer locks.
* The shared `requests.Session` retry / throttling policy that every Plex call goes through.
* Why Python threads are the right tool for this workload despite the GIL.
* Design tradeoffs and known sharp edges (single-engine worker, state centralisation, the Merge-mode "sum" semantic).
* Status and roadmap for Emby and Jellyfin support.

If you are contributing code, reviewing a change, or trying to understand why the engine made a particular choice, that is the document to read.

---

## Learning More

The `SOURCES.md` file in this folder contains links and plain-English explanations for every technology, library, and API used in this project. That includes FastAPI, uvicorn, Pydantic, React, Vite, TypeScript, Nginx, and Docker (added in v0.8.0). If you want to understand how Hestia-MediaManager works under the hood, what python-plexapi is, how MusicBrainz GUIDs work, what ThreadPoolExecutor does, or how Plex's scrobble API works, start there. Each entry explains not just what the technology is, but why it matters for this specific project.

**Project structure.** Hestia-MediaManager is split into the engine and the web layer that drives it. Engine logic lives under `services/`. The web layer lives under `server/` (FastAPI backend) and `frontend/` (React UI); together with `Dockerfile.backend`, `docker-compose.yml`, and `Makefile` they are the full running app.

<details>
<summary><strong>Full file layout</strong> (click to expand)</summary>

| File / Directory | What it contains |
|---|---|
| `services/state.py` | All shared module-level globals and singletons (VERSION, console, thread locks, counters). Per-run state lives in `ContextVar`s so fan-out destinations stay isolated. |
| `services/dashboard.py` | Dashboard UI, keyboard handling, Rich Progress factory. Holds `submit_with_context` which is how worker pools inherit the current run's context. |
| `services/logging_ops.py` | Logging setup, result recorders, log file writers. |
| `services/auth.py` | Token discovery, shared `requests.Session` with retry policy, server connection, library enumeration, home user fetching. |
| `services/resolver.py` | Item serialisation and four-tier matching (GUID -> filepath -> suffix -> fuzzy). |
| `services/snapshotter.py` | Full snapshot pipeline including `run_snapshot()`. |
| `services/restorer.py` | Full restore pipeline including `run_restore()` (Merge and Replace). |
| `services/timing.py` | Per-phase timing instrumentation for the dashboard's phase strip. |
| `services/tunables.py` | All operator-tunable knobs (worker counts, pool sizes, retry counts, throttles). |
| `server/app.py` | FastAPI app: REST routes for settings, libraries, jobs, schedules, logs, snapshots; WebSocket at `/ws/dashboard`. |
| `server/jobs.py` | Single-worker job queue that wraps `run_snapshot` / `run_restore` / direct-transfer. |
| `server/schedules.py` | Background scheduler thread for recurring snapshots. |
| `server/persistence.py` | Atomic JSON file I/O for `schedules.json`, `settings.json`, and `servers.json`. |
| `server/ws.py` | 4 Hz WebSocket broadcaster. Pushes `DashboardState.to_dashboard_frame()` to every connected browser. |
| `server/auth_db.py` | App-user identity, password hashes, JWT refresh tokens (separate from Plex auth). |
| `server/auth_router.py` | `/api/auth/*` routes: login, refresh, user management. |
| `server/media_db.py` | Schema and DML for `media.db` (the live working pool). |
| `server/snapshot_registry.py` | Reads / writes `snapshots.db` (the index of captured snapshot files). |
| `server/snapshot_capture.py` | Writes new snapshot `.db` files. |
| `server/snapshot_serializer.py` | Generates `.plexexport.json` sidecars on demand from a snapshot `.db`. |
| `server/snapshot_browser.py` | Read-only browse over `snapshots/`. |
| `server/log_browser.py` | Read-only browse over `plex_logs/`. |
| `server/log_scrubber.py` | Logging filter that strips Plex tokens, JWTs, Fernet ciphertexts, and passwords from every record. |
| `server/preflight.py` | The PIN preflight check that produces the at-risk user list. |
| `server/user_capture.py` | Throttled background token capture for managed users. |
| `server/secrets.py` | Fernet-based at-rest encryption for Plex tokens. |
| `server/server_registry.py` | CRUD for `servers.json`. |
| `server/managed_users_router.py` | `/api/servers/<id>/users/*` routes for the per-user PIN and token store. |
| `server/fan_out.py` | Multi-destination orchestration (one worker thread per destination, per-destination context isolation). |
| `server/runtime_patches.py` | Runtime monkey patches that put the engine into headless mode (no edits to `services/` source). |
| `server/models.py` | Pydantic request / response schemas. |
| `frontend/src/App.tsx` | React tab layout + WebSocket subscription. |
| `frontend/src/components/*.tsx` | One file per tab (Dashboard, Run Job, Schedules, Logs, Snapshots, Settings, Servers, etc.). |
| `frontend/src/api.ts` | Typed REST + WebSocket client. |
| `Dockerfile.backend` | Backend container image. |
| `frontend/Dockerfile` | Two-stage React build + nginx serve. |
| `frontend/nginx.conf` | SPA fallback + `/api` and `/ws` reverse proxy to the backend container. |
| `docker-compose.yml` | Two-service orchestration with host bind mounts. |
| `Makefile` | `make docker` and related convenience targets. |

</details>
