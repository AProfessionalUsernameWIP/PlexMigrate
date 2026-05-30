# Hestia Media Manager

**Version 0.18.0** (snapshot schema v18)

Hestia started as what it still fundamentally is at its core: a Python ETL service dressed up with a React frontend. That frontend exists for one reason, to take something that would otherwise require a terminal and put real, meaningful control in the hands of the end user. Not just control for the sake of it, but informed control. Every setting has context. Every change comes with an explanation of what it does and what it means. If you want to tune something, tune it. If you want to leave it at defaults, those defaults are sane and deliberate. This is your application running on your system. Hestia just ships with guardrails, not a leash.

That design philosophy shows up everywhere. Rather than hardcoding arbitrary limits, nearly everything in Hestia is dynamic and configurable. The reasoning is simple: I am going to think of a lot of things, but I am not going to think of everything, and I am not going to value every use case the same way you do. So instead of making those decisions for you, Hestia gives you the knobs and tells you what they do.

> **Three docs, three audiences. Pick the one that fits you.**
>
> * **[QUICKSTART.md](QUICKSTART.md)**: the 5-minute setup. Read this if you just want to get Hestia running and take a snapshot.
> * **README.md** (this file): the Hestia user manual and design tour. How Hestia thinks, how to install and configure it, run it, schedule it, troubleshoot it, and tune it.
---

## Why a React Frontend Over a Terminal

A terminal is limited in both functionality and interactivity. A well-designed UI does not just look better; it does actual work. By default, Hestia's frontend handles checksumming of data for you. More importantly, it is designed to impose logical guardrails at the input level, making it structurally difficult for a user to provide invalid data or trigger an operation in the wrong order. That is not a replacement for proper try/catch and error handling on the backend (you should always be prepared for edge cases), but the frontend should be doing everything it can to prevent those edge cases from being reachable in the first place.

---

## What It Captures

Hestia solves a problem that Plex, Emby, and Jellyfin users have faced for years: your watch history, ratings, playlists, and collections are siloed inside whichever server you happen to be running, and moving between them means losing everything. Hestia bridges that gap by acting as a neutral intermediary. It connects to your Plex, Emby, and/or Jellyfin servers, extracts your media metadata through each service's API, and stores it in a unified, server-agnostic database. From there, that data can be restored to any supported backend, cleanly, accurately, and without data loss.

For every user across every connected server, Hestia tracks:

- Watch history and play counts
- Ratings and favorites
- Playlists, including Plex Smart Playlists
- Collections, across all media types

The short version: **Plex does not have a backup button. Hestia is that button, and now it works for Emby and Jellyfin too.**

A few things worth knowing up front:

* **It runs alongside your server, not against it.** Hestia talks to each backend through the same API your phone app uses. Active streams keep playing. Nothing on the media disk is touched. You can run a snapshot while the family is watching TV and nobody will notice.
* **It never deletes anything by default.** Restore Merge mode (the default) is strictly additive: it only adds what is missing. There is also a destructive Replace mode for "I want this destination to be an exact mirror of a known capture," and it requires you to type a confirmation word before it will run.
* **It is built backend-agnostic at rest.** The data model does not bake in any one service's schema, which is why the same engine can speak Plex, Jellyfin, and Emby without forking the storage layer.

---

## The Translation Problem

Each backend speaks a slightly different language, and bridging them is where the real complexity lives.

Plex exposes a 0-10 numeric rating over the API (the UI shows it as 0-5 half-stars), and has no per-item favorite concept of its own. Jellyfin and Emby expose both a numeric `UserData.Rating` and a separate `IsFavorite` toggle. Same-backend transfers (Plex to Plex, Jellyfin to Jellyfin, Emby to Emby) preserve both faces of the affinity exactly.

Cross-backend is where the asymmetry shows up. Numeric ratings carry through cleanly in either direction: a Plex rating lands as `UserData.Rating` on Jellyfin/Emby, and a Jellyfin/Emby rating lands as `userRating` on Plex. The harder face is the favorite, because each direction has one side of the translation with no native equivalent. Hestia closes that gap with two Hestia-user-controlled tunables:

- **`favorite_threshold`** (default `5.0`): governs the **Plex source to Jellyfin/Emby destination** direction. Plex has no favorite flag of its own, so Hestia derives one from the numeric rating. Any rating at or above this cutoff also flips `IsFavorite=true` on the destination. A sub-threshold rating leaves the destination's favorite flag untouched rather than ever clearing a favorite the destination user set themselves.
- **`favorite_as_rating_value`** (default `10.0`): governs the **Jellyfin/Emby source to Plex destination** direction. A favorited item from J/E has no native landing spot on Plex's rating scale, so Hestia writes this numeric value to represent the favorite. The default treats a favorite as a maximum rating; drop it lower if you would rather favorites land as a softer "liked."

Both tunables have sane defaults; you only need to touch them if your taste for "what counts as a favorite" differs from the defaults.

Plex enforces typed playlists (a video playlist can only contain video, a music playlist can only contain music), while Emby and Jellyfin natively support mixed-media playlists. When a mixed playlist needs to land on a Plex destination, Hestia classifies the items and splits the playlist by type so nothing is silently dropped.

Plex Smart Playlists are filter-driven and dynamic, with no direct equivalent on either other platform. Hestia interprets the filter spec, runs a vocabulary preflight against the destination, and translates whatever it can into something meaningful at the other end.

Jellyfin and Emby share a common ancestor (Emby's pre-3.6 codebase that Jellyfin forked from), and most of the REST surface still overlaps. Hestia leans on that: the Emby adapter subclasses the Jellyfin adapter and only overrides the places the APIs have meaningfully diverged (auth header scheme, identity probe). Rather than forcing a lossy 1:1 mapping across all three backends, Hestia makes deliberate, documented translation decisions to preserve as much data fidelity as possible while being transparent about what changes in the process.

---

## API Constraints and Rate Awareness

Rather than naively hammering each server with requests, Hestia is designed to work within the natural constraints of each API: batching requests, respecting pagination, and pooling data in a way that avoids self-inflicted rate limiting or server strain. Understanding how each service surfaces its data over the API was a real investment of time, but getting it right meant the difference between a tool that chokes your server and one that runs cleanly in the background.

The Networking tab surfaces this directly. Per-server HTTP health, status-code histograms, and a live rate-limit feed are visible to you at any time, so when something is slow you can see whether the bottleneck is Hestia, the network, or the backend itself.

---

## Language and Architecture Choices

Python was chosen not for its performance or its threading model, but because it is one of the most accessible and widely understood languages available. The GIL is a real constraint for CPU-bound tasks, but Hestia is almost entirely network I/O, and the GIL doesn't sell you out there. It did not meaningfully slow anything down.

On the database side, Hestia currently uses SQLite, which works well for most setups. However, media libraries in the 10-40 TB range are not unusual, and the metadata volume at that scale will eventually outgrow what SQLite handles efficiently. A companion PostgreSQL container is being scoped for a future release (running as a Docker sidecar) for anyone intending to run Hestia long-term at scale. That migration would arrive as a standalone migration script, not an in-engine compatibility shim.

Hestia itself ships as two Docker containers: a FastAPI Python backend and an nginx-served React frontend, both wrapping the shared engine code under `services/`. The database layer is intentionally backend-agnostic, storing normalized metadata that is not tied to any one service's schema.

---

## Core Design Pillars

Efficiency, security, testing, data fidelity, and functionality: all required before this ships as a full release. Logging is verbose and intentional. The help system is thorough. The data viewing experience inside the UI is designed to surface as much information as possible, because an informed user makes better decisions than one flying blind.

Hestia is currently sitting at about 80% confidence. Final security and networking audits are in progress, and the full 1.0 release is the next milestone.

---

## Multi-User Access Control

This could have been a single-user app, but I used it as an opportunity to build a real multi-user schema and challenge myself on authentication design. There are six roles, five of which can log in and one of which exists as a second-factor credential pair for destructive database operations:

* **Viewer**: read-only access to the Dashboard and Servers tabs. No job execution.
* **Operator**: can run jobs and view the dashboards. No access to settings, tunables, or schedule editing.
* **Manager**: everything Operator has, plus the ability to stop running jobs and edit recurring schedules. Cannot change global tunables.
* **Admin**: nearly every permission, except the `settings.tunables` bundle (the infrastructure-knob surface) and the ability to modify a Root Admin row. Those two stay Root-Admin-exclusive on purpose.
* **Root Admin**: full permissions, sudo-style elevation, and access to the dev panels.
* **DB Admin**: a non-login credential pair used as a second factor on destructive database endpoints. Not a session role; a separate gate.

Auth is always on. On first boot the UI walks you through creating the Root Admin account; every subsequent boot shows the login screen. Plex tokens, Jellyfin/Emby API keys, and managed-user PINs are all encrypted at rest with a Fernet keyfile.

---

## Identity Mapping and User Impersonation

Connecting to a backend starts with pooling every user on that server: verifying their last connection time, confirming they are still active, and collecting their authentication token. Administrator credentials are required to access this data in the first place (an admin token on Plex, an admin API key on Jellyfin and Emby).

For managed users (primarily Plex Home users with PINs), Hestia collects usernames and tokens where possible. The complication is accounts protected by their own credential: Emby and Jellyfin admin accounts with passwords, and Plex Home users with PINs set. Without those credentials, Hestia would have to fall back to the administrator token, which means any playlists or collections restored to that account would not be manageable by the end user. To solve this properly, Hestia lets you store per-user passwords and PINs for each respective backend in the encrypted vault. When a restore, migration, playlist deployment, or direct transfer runs, Hestia authenticates as that user, deploys the artifacts under their identity, and ensures they have full ownership and control over what was restored.

On the Plex side this uses the real per-user token derived from the stored PIN. On Jellyfin and Emby, where the API allows it, the admin key is used with the destination `UserId` so the write is attributed to the user without holding a separate token.

This matters for the long-term usability of these backends. It also opens up a natural expansion: playlist sharing between users. If one user has a playlist they want another user to own (not just see, but actually manage), the user impersonation layer makes it possible.

### Understanding the cross-platform preflight modal and user mapping

When you run a restore, direct transfer, or playlist copy involving users from different servers (or servers of different types), Hestia shows an interactive modal on the Run Job form before the job starts. This **cross-platform preflight modal** lets you explicitly map users: "the Plex user 'Diesel' on Server A is the same person as the Jellyfin user 'diesel' on Server B." These mappings live in the **`user_identity_map`** table and are reusable across jobs and schedules.

If you do not make an explicit mapping, Hestia falls back to the 5-step resolution chain below, trying each method in order until it finds a match. The `strict_identity_resolution` tunable (defined later) lets you short-circuit that chain to require explicit maps for every user.

**PIN and password capture:** For Plex Home users with a PIN set, use the Servers tab's PIN preflight modal (in the Server Users block) to store the PIN before running a restore. This ensures playlists and collections restore under the right user's identity. Jellyfin and Emby admin accounts with passwords follow the same pattern: capture the password in the Servers tab before the job runs. Stored credentials are encrypted at rest.

### How users are identified across servers (the app_user_uuid)

Hestia tracks three identifiers per user, each with a different lifetime and scope:

| Identifier | What it is | Mutable? |
|---|---|---|
| `user_handle` | Backend username (Plex username, Jellyfin Name, Emby Name) | Yes, if the username is renamed on the backend |
| `backend_user_id` | Backend-assigned per-server stable id (Plex.tv numeric userID; Jellyfin / Emby GUID) | Usually stable; can rotate on some backends |
| `app_user_uuid` | App-generated canonical anchor: the validation handle this app keys off | **No**: immutable for the lifetime of the row |

Every user added to the app gets an `app_user_uuid` generated at insert time. That covers every path a user can enter the app: managed-user sync, snapshot capture, snapshot restore, the User Management endpoints, the per-user-token save endpoint, and inline cross-platform user creation. There is no path that adds a user without a UUID.

The cross-server `user_identity_map` keys off two `app_user_uuid` values, so an explicit mapping from the preflight modal (e.g., "Plex Diesel" is the same person as "Jellyfin diesel") survives renames on either side, `backend_user_id` rotation (when a backend reassigns its internal user IDs), and even a backend being re-registered. Snapshot restore, direct transfer, and playlist copy all walk the same 5-step resolution chain for each source user's payload:

1. Per-job override (Map decision from the cross-platform preflight modal).
2. `user_identity_map` lookup (authoritative).
3. `backend_user_id` direct match within the same service type.
4. Case-insensitive username match (the legacy fallback).
5. Owner-role single-admin fallback (when the source user is the account owner and the destination has exactly one admin user). Used only when no other tier matches.

The `strict_identity_resolution` tunable cuts the chain short after step 2 for Hestia users who want every routing to come from an explicit map. When `strict_identity_resolution` is enabled, steps 3-5 are skipped; the preflight modal mapping (step 1) or an existing `user_identity_map` row (step 2) becomes mandatory for every cross-server user.

### Tombstoning: skipping users you do not want Hestia to touch

Tombstoning marks a managed user as "do not enumerate, do not write to" so Hestia stops seeing them across every code path that reaches into a server (home-user enumeration, restore preflight, direct transfer, playlist copy, mirror sync). The user still exists on the backend. Both flavors are reversible from the UI.

- **Manual (Hestia-User driven).** Servers tab > User Management. Choose per-server scope (this one server only) or global scope (every registered server). Use when an end user has churned off your stack and you do not want their roster row reached for.
- **Auto (background sweeper, default OFF).** A daemon polls each server on a configurable cadence and auto-tombstones users who fail the consecutive-auth-probe threshold. A 5-layer opt-in in Settings (sweeper, engine filter, auth-error trigger, unreachable trigger) gates the behavior; leave the unreachable trigger off unless your LAN to the backend is stable, since one network blip can rack up failures for every user at once.

---

## Collection Processing and Deduplication

Collections required some careful thinking. The first question we ask is: was this collection auto-generated? If yes, there is no value in copying it. The deeper problem is that global collections on a server apply universally. If a server has 300 global collections, every user technically "has" 300 collections. A naive read would process 300 collections per user, all identical, all redundant.

Instead, Hestia pulls the full list of global collections once, then does a fast comparison against each user's collection list to isolate what is actually private and user-owned. We cannot exclude global collections from the initial API read (that is a limitation of how these backends surface the data), but we make sure they are excluded from the snapshot. Only private, user-created collections get recorded and restored. Global auto-generated collections are ignored entirely.

A three-layer optimisation makes this fast at scale: an early-exit when a user has no personal collections at all, a skip-before-work pass that drops library-wide entries before the expensive items-fetch, and an optional fast-detection path that reads `librarySectionUserID` on each collection to decide ownership without a set lookup. The corresponding **Skip collections**, **Skip playlists**, and **Fast collection detection** settings in the Engine section of the Run Job form are documented in detail under "Tips for Large Libraries" further down.

---

## Performance Targets

Snapshot, restore, and direct transfer operations target a sub-10-minute completion window on small-to-medium libraries and have hit that target in the Windows and Ubuntu environments we run regularly. Larger libraries (deep music collections, many home users, hundreds of library-wide collections) take longer, and the "Tips for Large Libraries" section below covers how to plan an overnight run when that is the right call.

---

## Data Safety

Hestia has two restore modes (Merge and Replace), and Merge itself offers two sub-strategies for how view counts get reconciled. The defaults are conservative; the destructive Replace mode requires you to type a confirmation word before it will run.

### Merge mode (the default, additive only)

**Hestia never deletes, overwrites, or reduces any data on your target server in Merge mode.** Every restore is strictly additive. It only adds what is missing.

For watch counts specifically, Merge has two strategies you pick at submit time:

- **Merge / Higher** (default): the destination's final view count is `max(stored, current)`. If the destination already has a higher count, Hestia leaves it alone; if the snapshot has a higher count, Hestia adds only the delta. Idempotent across re-runs, so running the same Merge / Higher restore twice produces the same result the second time. Best for "I just want this server to know about my watch history."
- **Merge / Sum** (combined totals): the destination's final view count is `current + stored`. Every captured play is treated as a real event and added on top, even when the destination already has independent play counts of its own. Use this when you are intentionally combining the playback history of two servers you have actually been watching on (for example, a primary server and a travel server you used in parallel).

The rest of the data types behave the same way regardless of which strategy you pick:

- **Watch history (other than counts)**: Resume positions are only restored if the destination has no saved position for that item.
- **Playlists**: If a playlist with the same name already exists, the script adds any items that are missing from it. Items already present are skipped. The playlist is never deleted or replaced. Descriptions are never overwritten. Item order from the original playlist is preserved.
- **Collections**: Same as playlists. Existing collections get missing members added, and nothing is removed.
- **Ratings**: If an item on the new server already has a rating, the script skips it. Your rating on the new server always wins.

You can safely run a Merge / Higher restore multiple times on the same server: it will not create duplicates or inflate your data. Repeated Merge / Sum runs against the same destination **will** keep adding view counts every time (that is the point of Sum), so reserve Merge / Sum for the actual "combine two real histories" case rather than as a default cadence.

### Replace mode (opt-in, destructive)

Replace mode makes the destination match the snapshot exactly. It re-scrobbles watch counts to the snapshot value (so it can lower them, not just raise them), overwrites ratings, and recreates playlists and collections from scratch. The UI requires you to type the word `REPLACE` before it will submit the job, and a pre-replace safety-belt snapshot is captured into `snapshots.db` automatically so you can roll back if you do not like the result. If the safety belt fails to capture, the destructive write does not fire.

### Picking the right mode

- **Merge / Higher** for "I am migrating to a new server" or "I want to keep both sides going" or "I just want this server to know what I have watched." Safe to re-run.
- **Merge / Sum** for "I have actually been using two servers in parallel and want their playback history combined on this one." Do not re-run against the same destination unless you really do want to add the counts again.
- **Replace** for "I want this destination to be an exact mirror of a known-good capture." Only after you have practised once on a non-critical library.

---

## Where Hestia keeps its state

Hestia uses three small SQLite files under `server_data/`. You usually do not need to touch them directly, but knowing what each one stores makes backups and troubleshooting much easier:

| File | What it stores |
|---|---|
| `auth.db` | App user accounts, password hashes, and JWT refresh tokens. Nothing about Plex/Jellyfin/Emby itself. |
| `media.db` | The live working pool of every metadata item, watch event, rating, playlist, and collection Hestia has ever ingested. |
| `snapshots.db` | An index of every captured snapshot `.db` file on disk, with per-server retention metadata. |

Each one can fail or be rebuilt independently of the others, which is why the tool is safe to leave running for months. The architectural choices behind the split (WAL mode, single-writer pattern, GUID keying) are documented in code comments alongside each database's initialisation.

---

## Quick Start

A Docker tool to back up and restore your media-server user experience across servers and across backends. Preserves watch history, playlists, ratings, and collections via each backend's native API. No downtime, no database access.

### Requirements

Docker Desktop (macOS / Windows) or Docker Engine + Compose v2 (Linux). No Python or Node needed on the host. Your media server should be reachable from the backend container. The default Plex URL is `http://host.docker.internal:32400`; `host.docker.internal` is mapped to the host gateway in `docker-compose.yml`, so this works on Linux too.

### 1. Bring up the stack

```bash
docker compose up --build
```

This builds and starts two containers: a FastAPI backend on `localhost:8000` (loopback-only, holds your credentials) and an nginx frontend on `localhost:8080` (the web UI). First boot can take a minute or two while it pulls images and builds.

> You do not need to run this on the same machine as your media server. Any machine on the same network works; just point Hestia at the server's IP later.

### 2. First-boot setup (one time only)

Open **http://localhost:8080** in your browser. On a fresh install you will see a **Setup** page, not a login page. This is the always-on auth wizard. Hestia requires authentication for every API call, so you have to create an admin account before you can use anything.

1. Pick a username and password for your Root Admin account.
2. Click **Create root admin**.
3. You are redirected to the login page. Sign in with the credentials you just created.

After this first-boot step, every subsequent visit goes straight to the login page. Tokens are 24-hour-lived; close the tab to log out.

### 3. Add your first server

Once logged in, you will land on the Dashboard. Add your media server before you can snapshot anything:

1. Click the **Servers** tab in the top nav.
2. Click **+ Add Server**.
3. Pick the backend type: **Plex**, **Jellyfin**, or **Emby**.
4. Enter the server URL (e.g. `http://192.168.1.100:32400` for a typical Plex on your LAN) and the access token or API key. For Plex, see [the Plex token guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) if you do not know yours. Jellyfin and Emby use API keys generated from their dashboards.
5. Click **Save**. The backend probes the server, fetches the library list, and the row appears in the Servers tab with a green status pill if everything is good.

Add as many servers as you need.

### 4. Take a snapshot

1. Click the **Jobs** tab.
2. The mode pill defaults to **Snapshot** (the others are Restore and Direct).
3. Pick a source server from the dropdown.
4. Pick which libraries to capture (Movies, TV Shows, Music, etc).
5. Click **Submit job**.
6. The Dashboard takes over with live items/sec, ETR (estimated time remaining), and per-library progress. When it finishes, the snapshot lands in `./snapshots/` on the host and shows up in the Recent Runtimes table.

### 5. Restore to a different server (or the same one)

1. Add the destination server using the same flow as step 3.
2. Click the **Jobs** tab.
3. Switch the mode pill to **Restore**.
4. Pick the source snapshot from the list (or upload a `.db` file if you have moved it from another machine).
5. Pick the destination server.
6. Choose merge mode: **Merge / Higher** (take whichever viewCount is higher, safest), **Merge / Sum** (add the counts together), or **Replace** (overwrite the destination, requires typing REPLACE to confirm, and optionally auto-captures a safety snapshot of the destination first).
7. Click **Submit job**.

---

## Deployment and Run Commands

### Docker commands

| Command | What it does |
|---|---|
| `docker compose up --build` | Build and start everything |
| `docker compose up -d` | Same, detached (run in background) |
| `docker compose down` | Stop containers, keep your data |
| `docker compose down -v` | Stop containers and wipe volumes (does NOT touch host bind mounts) |
| `docker compose logs -f backend` | Tail backend logs in real time |
| `docker compose restart backend` | Restart just the backend after a code change |

### Makefile convenience targets

| Target | What it does |
|---|---|
| `make docker` | Runs `docker compose up --build`. Builds and starts the full web stack on `http://localhost:8080`. |
| `make docker-rebuild` | Forces a no-cache backend rebuild then starts everything. Use after a Python source change when you want to be certain the container picked up the new code. |
| `make clean` | Removes `./venv/` if one exists. Does not touch `snapshots/`, `plex_logs/`, or `server_data/`. |
| `make help` (or just `make`) | Prints the target list. |

### Where data lives

Three host directories are bind-mounted into the backend container so all data outlives the container lifecycle:

| Host path | Container path | Contents |
|---|---|---|
| `./snapshots/` | `/app/snapshots/` | Per-server snapshot `.db` files. `snapshots/legacy/` holds any pre-PR-13 `.plexexport.json` archives moved there by the first-boot migration. |
| `./plex_logs/` | `/app/plex_logs/` | Per-run log directories |
| `./server_data/` | `/app/server_data/` | `settings.json`, `schedules.json`, `servers.json`, `media.db`, `snapshots.db`, `auth.db`, and the binary `.keyfile` used for at-rest encryption |

Back up `./server_data/` if you want to preserve your registered servers and credentials. Snapshots can always be regenerated; the registry cannot. The `.keyfile` is 32 raw bytes: do not open it in a text editor, do not commit it to source control (`.gitignore` already excludes it), and do not delete it unless you are prepared to re-enter every registered server's token.

### What the web UI gives you

* **Dashboard**: the live view of the current run. Header shows the libraries queued, the library currently being processed, and (when a direct transfer is scoped to specific users) which user the engine is on. Below that: thread pool counts, run stats, match resolution stats, per-library progress bars with ETA, the colour-coded activity feed, and a Network Activity panel that charts HTTP status codes, requests-per-second, and average latency over the last 60 seconds. Updates over a WebSocket at 4 Hz. A **Hard Stop (force)** button is available for runs that need to be cut off immediately.
* **Run Job**: every per-run option has a clearly labelled form control. Pick snapshot, restore, or direct server-to-server transfer; select libraries (or snapshot rows); set worker count; toggle verbose and strict match; fill in path remap if needed; then submit. Direct mode adds a Users section with checkboxes for the owner and every managed user that exists on both servers. The Destination Server picker supports multiple targets for fan-out. Jobs run one at a time; subsequent submissions queue.
* **Servers**: register, edit, test, and remove servers. Includes the Server Users block, library list, status pill with response time, and (for Plex Home) per-user PIN capture so playlists and collections restore under the right identity.
* **Schedules**: create, edit, enable/disable, and delete recurring snapshot schedules. Schedules fire in the container's configured timezone (set the `TZ` env var in `docker-compose.yml`). The topbar shows a live server-time clock so you always know what time the schedule engine sees.
* **Networking**: per-server HTTP health, status-code histograms, rate-limit feed.
* **Logs**: a three-pane browser over `plex_logs/`. The viewer has a case-insensitive keyword filter; matching lines stay visible with the match highlighted. Files larger than 4 MB show the tail.
* **Exports / Snapshots**: the snapshot registry, grouped by server, with download, retention controls, and a Legacy JSON archives section for any pre-PR-13 `.plexexport.json` files relocated to `snapshots/legacy/` on first boot.
* **Settings**: backend URLs, encrypted tokens (write-only, never echoed back), and the default values for every per-run option. The Tunables surface is Root-Admin-only.
* **Dev Console** (Root Admin, gated by `PLEXMIGRATE_DEBUG_MODE=1`): Developer Panel, Application Logs, Databases, Server Commands, Drift History, Sync Activity, and Dev Blog tabs for deep inspection.

---

## Multi-Server Support

Hestia manages a registry of servers rather than a single connection. Every snapshot, restore, and schedule targets a specific registered server by friendly name. Direct transfer mode moves data from one registered server straight into another in memory; fan-out (v0.10.0) lets one source feed many destinations in parallel, each on its own thread, each with its own per-destination cards and per-library logs.

When the in-memory direct path fails for a library mid-transfer, Hestia automatically falls back to a temporary snapshot-and-restore for that library only. The other libraries keep going on the direct path.

For renamed libraries, the **Library Mapping** tab lets you wire up "Movies" on source to "Films" on destination so the engine routes items correctly. Mappings are reusable across schedules and per-run overrides.

> **Removing a server from the registry never deletes any snapshot files or log directories produced from that server.** The registry is just a pointer table. The files on disk live independently.

---

## Smart Playlists

Smart playlists are playlists whose contents are generated by a saved filter (for example, "all unwatched Action movies added this year"). Hestia's dedicated Playlist Transfer flow (and the broader Smart Playlist migration job type) parses the filter spec, runs a vocabulary preflight against the destination, and translates whatever it can.

Where a filter cannot be translated (server-specific id, missing destination vocabulary), Hestia:

1. Records the playlist in the failure log with the category "Smart Playlist - Requires Manual Recreation."
2. Saves the original filter URL in the run log for reference.
3. Does not create a placeholder on the destination.

To restore a smart playlist manually: open the destination, create a new Smart Playlist, and re-enter the same filter criteria using the URL from the run log entry.

---

## Plex Home Users

If your Plex server is linked to a Plex.tv account and runs Plex Home (multiple user profiles), the identity layer described in [Identity Mapping and User Impersonation](#identity-mapping-and-user-impersonation) (see the [5-step resolution chain](#how-users-are-identified-across-servers-the-app_user_uuid) for details) does the heavy lifting automatically. This section covers the two Plex-Home-specific operator behaviors you'll see in the UI.

**Prerequisite.** The server must be linked to a Plex.tv account (not running on a LocalAdminToken). Without the Plex.tv link Hestia has no way to enumerate managed users on that server.

**Per-user PIN preflight.** Before a job submits, if any managed user has a Plex Home PIN set and Hestia has no captured token for them, the Run Job form lists those at-risk users. Two recovery paths: capture the PIN in the Servers tab via the PIN preflight modal and re-run, or continue and have those users skipped with a clear log line.

**Missing-user gap report.** At job start, Hestia logs which managed users it found on the target server and which exist only in the snapshot, so you can see the gap immediately. Users not found on the target are skipped with an INFO log, not an error. The end-of-restore summary lists by name which users were restored and which were skipped.

---

## Step-by-Step: Migrating to a New Server

1. **On your old server:** Run the snapshot from the Run Job tab in the web UI. Select the libraries you want to back up.
2. The script creates a per-server snapshot `.db` (and optionally a `.plexexport.json` sidecar) in the `./snapshots/` folder, one per library.
3. **Copy those files** to the machine where your new server runs. USB drive, network share, cloud storage; any method works.
4. **On your new server:** Make sure your media files are accessible and the backend has scanned them. The items must appear before you can restore.
5. Run the restore, pointing at the snapshot files.
6. Check the `./plex_logs/` folder for a summary and any items that need manual attention.

---

## Log Files

All logs land in `./plex_logs/` (or the path you set in Settings). Filenames carry a timestamp so runs never overwrite each other.

| Log file | When created | What is in it |
|---|---|---|
| `run_YYYYMMDD_HHMMSS.log` | Always, one per run | Full transcript: startup, library discovery, every action, all successes and failures, final summary. Start here when something goes wrong. Add `--verbose` for DEBUG detail. |
| `{LibraryName}_success_YYYYMMDD_HHMMSS.log` | At least one item in that library succeeded | Every successful item, the matching method (GUID lookup, file path, or title search), and the action taken. Action tags: `[CREATED]`, `[APPENDED]`, `[RATING SET]`, `[SKIPPED - ...]`. Ends with a totals summary and success rate. |
| `{LibraryName}_fail_YYYYMMDD_HHMMSS.log` | At least one item in that library failed | Every failed item, the GUID and file path tried, and the specific reason. Same summary block as the success log. |
| `troubleshoot_YYYYMMDD_HHMMSS.log` | Any failures occurred | Failures grouped by category (file not found, ambiguous title match, etc.) with a plain-English explanation and step-by-step fix, plus a "Next Steps" section. |
| `unresolved_YYYYMMDD_HHMMSS.log` | Items failed all matching tiers | One-line-per-item checklist for manual restoration, with a short intro explaining what to do with the file. |

---

## Common Problems

If you hit an issue not covered by this README, please file it on the issue tracker and include the relevant log snippet from `plex_logs/`. The most common failure modes have obvious causes in the logs: `externally-managed-environment` pip errors, missing modules, the "local:// GUID" music-track edge case, the empty-`guids` IndexError in Play Count restore, and the playlist 400 bad_request on large static playlists.

The first place to look is the run log directory under `plex_logs/run_<slug>_<timestamp>_FAIL/`, which carries the per-library success/fail logs, the runtime/errors/media streams, and a generated `troubleshoot.log` keyed by failure category.

---

## Tips for Large Libraries

- Set the worker count to 16 or higher on machines with many CPU cores to speed up processing.
- Run the snapshot overnight if your library is very large. Hestia is safe to leave running.
- After restore, check the dashboard to verify watch history appears correctly on a few items before assuming everything is done.
- You can safely run the restore more than once. The additive merge logic means repeated runs only add what is still missing.
- The live Dashboard tab shows every run in real time: thread pool counts, per-library progress bars with ETA, run stats, match resolution breakdown, and a colour-coded activity feed.

### Collection performance settings 

Collections are one of the heaviest parts of a snapshot or direct transfer when you have many home users. On a server with 12 users and 300 library-wide collections, the naive approach sends 3,600 API calls just for collections, even though all 300 are visible to everyone. The three-layer optimisation described above is automatic; two opt-in toggles tune behaviour further:

| Setting | What it does | When to use it | When to leave it off |
|---|---|---|---|
| **Skip collections** | Omits the collection gather entirely. Watch history, playlists, and ratings transfer normally. | You do not need collections at the destination, or the destination will build its own. Fastest possible transfer. | You have personal collections you want to preserve across servers. |
| **Skip playlists** | Omits the playlist gather entirely. Watch history, collections, and ratings transfer normally. | Migrating to a fresh server and you would rather rebuild playlists by hand, or most of your playlists are smart playlists. | You have regular (non-smart) playlists you want preserved at the destination. |
| **Fast collection detection** | Reads `librarySectionUserID` on each collection to decide ownership without a set lookup. `None`/`0` = library-wide (skip); any other value = personal (process). | You are on **Plex Media Server >= 1.32** with 100+ library-wide collections and/or 5+ home users. | You are on an older Plex build. The engine falls back automatically if the attribute is missing, so it is safe to enable, but you gain nothing on old servers. |

**Rule of thumb:**
- Fewer than 5 home users and/or fewer than 50 collections: the built-in three-layer optimisation is already fast enough.
- 10+ home users **and** 200+ library-wide collections: enable **Fast collection detection**.
- Speed-first migration, rebuilding collections later: enable **Skip collections**.
- Mostly smart playlists: enable **Skip playlists**. They would all fail anyway.

---

## Understanding Performance and Threading

All worker threads run on the host (the machine running the container), **not** on your media server. Speed depends on your network to the backend, how fast the backend answers, and how many parallel requests it tolerates. The default thread count comes from the host's CPU core count, but Hestia spends almost all of its time waiting on the backend, so the host's core count barely matters.

> **A note for the technically curious.** If you have heard that Python's GIL prevents Python from using more than one CPU core, that is true and almost completely irrelevant for this tool. Hestia spends roughly 99% of its wall-clock time waiting for HTTP responses or for SQLite to fsync, and both of those waits release the GIL. A 16-worker pool against a backend really is 16x parallel for the part that matters.

### If it is running slowly

1. Lower the worker count in the Worker threads field (Run Job tab).
2. Get on the same LAN as the backend. Single-digit milliseconds on a LAN, hundreds over the internet.
3. Check whether the backend is busy streaming or transcoding. That contention shows up as slow API responses.

### If it is running well and you want it faster

Increase the worker count, but watch the **Failed** count in the dashboard. Failed items mean the backend did not answer in time or returned an error. If Failed climbs, the backend is overwhelmed; drop workers back down.

### Direct server-to-server transfer

Both servers receive API calls at once, so cut workers further to keep both responsive. The Run Job tab shows a banner reminding you of this when you pick direct transfer mode.

### Recommended starting points

| Scenario | Recommended worker count | Notes |
|---|---|---|
| Host and backend on the same LAN | The default (`min(32, cpu_count x 4)`) | Network is fast and reliable. The backend itself is the bottleneck. |
| Host remote from the backend / over the internet | Start with 8 and increase from there | High round-trip time means more parallel requests in flight, not all of which can be served before timing out. Fewer workers = fewer timeouts. |
| Direct server-to-server transfer | Half the default (`min(16, cpu_count x 2)`) and monitor both servers | Both servers feel the load. The lower starting point gives you headroom to increase if both stay healthy. |

### Registering many servers

Every snapshot adds load to whichever server it reads from. Stagger your scheduled windows so two schedules do not fire at the same minute against the same backend. Large music libraries hit the API hardest, so give those breathing room. The Servers tab shows a banner reminding you of this when you have multiple servers registered.



## Learning More

The `SOURCES.md` file in this folder contains links and plain-English explanations for every technology, library, and API used in this project. That includes FastAPI, uvicorn, Pydantic, React, Vite, TypeScript, Nginx, and Docker. If you want to understand how Hestia works under the hood, what python-plexapi is, how MusicBrainz GUIDs work, what ThreadPoolExecutor does, or how the scrobble API works on each backend, start there. Each entry explains not just what the technology is, but why it matters for this specific project.

**Project structure.** Hestia is split into the engine and the web layer that drives it. Engine logic lives under `services/`. The web layer lives under `server/` (FastAPI backend) and `frontend/` (React UI); together with `Dockerfile.backend`, `docker-compose.yml`, and `Makefile` they are the full running app.

<details>
<summary><strong>Full file layout</strong> (click to expand)</summary>

| File / Directory | What it contains |
|---|---|
| `services/state.py` | All shared module-level globals and singletons. Per-run state lives in `ContextVar`s so fan-out destinations stay isolated. |
| `services/dashboard.py` | Dashboard UI, keyboard handling, Rich Progress factory. Holds `submit_with_context` so worker pools inherit the current run's context. |
| `services/logging_ops.py` | Logging setup, result recorders, log file writers. |
| `services/auth.py` | Token discovery, shared `requests.Session` with retry policy, server connection, library enumeration, home user fetching. |
| `services/resolver.py` | Item serialisation and four-tier matching (GUID, filepath, suffix, fuzzy). |
| `services/adapters/` | Per-backend adapter modules. `jellyfin.py` is the shared base; `emby.py` subclasses it; `plex.py` (with the `_plex_*.py` mixins) carries the Plex-specific writes. |
| `services/snapshot/` | Snapshot job package. `plex_native/snapshotter.py` is the Plex-direct engine; `adapter/snapshotter.py` is the cross-backend engine via `MediaServerAdapter`. |
| `services/restore/` | Restore job package. `plex_native/` holds the Plex-direct engine (`engine.py`, `watch.py`, `playlists.py`, `collections.py`, `ratings.py`); `adapter/` holds the cross-backend engine (`restorer.py`, `preflight.py`); `replace_sweep.py`, `restoration_log.py`, and `mixed_media.py` are restore-affiliated helpers shared by both engines. |
| `services/direct_transfer/engine.py` | Direct server-to-server transfer orchestrator. |
| `services/fan_out/coordinator.py` | Multi-destination orchestration (one worker thread per destination). |
| `services/playlist_copy/` | Playlist copy job package. `adapter/copy.py` is the main orchestrator; `adapter/cache_api.py`, `adapter/cache_refresher.py`, `adapter/item_resolver.py`, `adapter/user_auth.py`, `adapter/batch_runner.py` cover the cache, resolution, auth, and batch surfaces; `log.py` is the playlist-cache audit log. |
| `services/smart_playlist/` | Smart-playlist filter translation across backends. `filter_model.py` holds the model; `log.py` is the audit sibling. |
| `services/mirror_sync/` | Server Mirror / write-through subsystem. `server_mirror.py` orchestrates; `sync_worker.py` is the subscription-driven sync engine; `writethrough.py` is the post-snapshot warmer; `log.py` is the audit log. |
| `services/collection_cache/` | Plex-only collection-cache walker. `warmer.py` runs the walk; `log.py` is the audit log. |
| `services/user_management/` | User-admin surface: `labels.py` (Plex owner labels), `creation.py` (cross-backend user creation), `activity_filter.py` (per-server active-user chokepoint), `activity_sweeper.py` (background auth-health probe), `activity_log.py` (audit writer). |
| `services/identity/` | User-identity bedrock: `user_uuid.py` (app_user_uuid generator), `user_resolution.py` (5-step destination resolution), `user_display.py` (display-name substitution), `plex_owner_identity.py` (SystemAccount owner-skip), `plex_shares.py` (Plex.tv share-state). |
| `services/translation/` | Pure cross-backend conversion: `guid_translator.py` (GUID schemes between Plex/Jellyfin/Emby), `backend_translation.py` (rating-vs-favorite affinity). |
| `services/library_mapping/` | Renamed-library wiring: `mapper.py` is the auto-match engine; `lookup.py` is the read helper. |
| `services/run_logs/` | Per-run sidecar log writers: `db_access.py`, `run_settings.py`, `run_timer.py`. Distinct from `services/logging_ops.py` (global logger setup). |
| `services/admin/` | Operator-only admin surfaces: `dev_console.py` (root-admin Server Commands). |
| `services/snapshot_validator.py` | Snapshot integrity validation (pre-capture and pre-restore). |
| `services/tunables.py` | All Hestia-user-tunable knobs (worker counts, pool sizes, retry counts, throttles). |
| `server/app.py` | FastAPI app: REST routes for settings, libraries, jobs, schedules, logs, snapshots; WebSocket at `/ws/dashboard`. |
| `server/jobs.py` | Single-worker job queue wrapping the snapshot / restore / direct-transfer engines under `services/`. |
| `server/schedules.py` | Background scheduler thread for recurring snapshots. |
| `server/persistence.py` | Atomic JSON file I/O. |
| `server/ws.py` | 4 Hz WebSocket broadcaster. |
| `server/auth_db.py` | App-user identity, password hashes, JWT refresh tokens. |
| `server/auth_router.py` | `/api/auth/*` routes: login, refresh, user management, elevation. |
| `server/dev_console_router.py` | Root-Admin Dev Console surface. |
| `server/media_db/` | Schema and DML for `media.db`. |
| `server/snapshot_registry.py` | Reads/writes `snapshots.db`. |
| `server/snapshot_capture.py` | Writes new snapshot `.db` files. |
| `server/snapshot_serializer.py` | Generates `.plexexport.json` sidecars on demand. |
| `server/log_browser.py` | Read-only browse over `plex_logs/`. |
| `server/log_scrubber.py` | Logging filter that strips tokens, JWTs, Fernet ciphertexts, and passwords from every record. |
| `server/preflight.py` | PIN preflight check producing the at-risk user list. |
| `server/user_capture.py` | Throttled background token capture for managed users. |
| `server/secrets.py` | Fernet-based at-rest encryption for tokens and PINs. |
| `server/server_registry.py` | CRUD for `servers.json`. |
| `server/managed_users_router.py` | `/api/servers/<id>/users/*` routes for the per-user PIN and token store. |
| `frontend/src/App.tsx` | React tab layout + WebSocket subscription. |
| `frontend/src/components/*.tsx` | One file per tab (Dashboard, Run Job, Schedules, Logs, Snapshots, Settings, Servers, Networking, Library Mapping, Access Control, Dev Console, etc.). |
| `frontend/src/api.ts` | Typed REST + WebSocket client. |
| `Dockerfile.backend` | Backend container image. |
| `frontend/Dockerfile` | Two-stage React build + nginx serve. |
| `frontend/nginx.conf` | SPA fallback + `/api` and `/ws` reverse proxy to the backend container. |
| `docker-compose.yml` | Two-service orchestration with host bind mounts. |
| `Makefile` | `make docker` and related convenience targets. |

</details>

---

## Links

* [QUICKSTART.md](QUICKSTART.md) - 5-minute setup
* [SOURCES.md](SOURCES.md) - every technology, library, and API used, with plain-English explanations
* [The Plex token guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

