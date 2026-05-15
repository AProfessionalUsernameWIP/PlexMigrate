# PlexMigrate -WIP
**Version 0.12.3**
A tool that moves your Plex watch history, listening history, playlists, collections, and star ratings between Plex servers, without losing any data. 

> **In a hurry?** See [QUICKSTART.md](QUICKSTART.md) for the 5-minute version. This README is the long reference.

PlexMigrate ships two run modes. **The Docker + Web UI is the recommended way to use it**, and the path that gets ongoing testing and feature work. The terminal CLI is still here for users who prefer a shell, but it is slowly drifting out of step with the web mode: it does not get the same test coverage, so it is more likely to carry bugs or feel rougher to use over time. **If you hit something broken in terminal mode, please open an issue and I will do my best to address it.**

* **Docker + Web UI (recommended).** `docker compose up --build` (or `make docker`). Brings up a FastAPI backend and a React frontend in two containers. The web dashboard shows live job state; every CLI flag has a form control; you can save scheduled recurring exports. This is the path that gets ongoing test coverage and feature work. Jump to [Docker and Web UI](#docker-and-web-ui).
* **Terminal mode (legacy).** `python plexmigrate.py`. The original CLI, with the live `htop`-style dashboard and keyboard shortcuts, no server, no Docker. Still functional, but no longer the focus of testing or new features. Jump to [How to Run](#how-to-run).

**Latest changes (v0.12.3): snapshot rename and capture pipeline.**
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

For older releases, see [versionhistory.md](versionhistory.md).


---

## What This Does

PlexMigrate works through Plex's built-in API, the same interface your Plex app uses when you hit play, mark something watched, or build a playlist. It doesn't touch your media files, move any data on disk, or require you to stop using Plex while it runs. You can keep watching TV or listening to music on any device while an snapshot or import runs in the background.

There are two steps.

**Snapshot.** Run this on your old server, or before you rebuild. PlexMigrate connects to Plex, reads your watch history, resume positions, star ratings, playlists, and collections, and saves them to a set of `.plexexport.json` files (one per library). Plex must be running on that machine for this step.

**Restore.** Run this on your new or freshly rebuilt server. PlexMigrate reads the export files and restores everything it can find, matching each item using four methods in order: by its global ID, by its exact file path, by a path-suffix match (for cross-platform migrations, see below), and finally by title. Plex must be running on the target machine for this step, but nothing else needs to stop. Active streams and in-progress playback are not affected.

Between those two steps, the export files are just files on disk. Copy them however you like (USB drive, network share, cloud storage) and run the import whenever you're ready.

PlexMigrate logs everything it does and attempts produces a plain-English troubleshooting report for anything it couldn't restore automatically.

---

## Data Safety

**PlexMigrate never deletes, overwrites, or reduces any data on your target server.** Every import is strictly additive. It only adds what is missing.

Here is what "additive" means for each data type:

- **Watch history**: If an item on the new server already has a higher view count than the export, the script leaves it alone. It only adds views when the export count is strictly higher. Resume positions (where you paused) are only restored if the new server has no saved position for that item.
- **Playlists**: If a playlist with the same name already exists, the script adds any items that are missing from it. Items already present are skipped. The playlist is never deleted or replaced. Descriptions are never overwritten. Item order from the original playlist is preserved.
- **Collections**: Same as playlists. Existing collections get missing members added, and nothing is removed.
- **Ratings**: If an item on the new server already has a star rating, the script skips it. Your rating on the new server always wins.

You can safely run PlexMigrate multiple times on the same server. It won't create duplicates or reduce your data.

---

## Docker and Web UI

Starting in v0.8.0 PlexMigrate ships an optional web layer. Nothing about the terminal mode changes. Running `python plexmigrate.py` still does exactly what it did in v0.7.1. The web layer is a separate codepath under `server/` (FastAPI backend) and `frontend/` (React UI) that wraps the same engine.

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

Once both containers are healthy, open <http://localhost:8080> in your browser. Open the **Settings** tab and paste your Plex server URL and authentication token (the same token the CLI auto-discovers from `Preferences.xml`). Settings persist on a host bind mount (`./server_data/settings.json`).

### What the web UI gives you

* **Dashboard tab**: the live, browser-side version of the terminal dashboard. The header now also shows the libraries queued for this run, which library is being processed right now, and (when a direct transfer is scoped to specific users) which user the engine is on. Below that you get the thread pool counts, run stats, match resolution stats, per-library progress bars with ETA, the colour-coded activity feed, and a new Network Activity panel that charts HTTP status codes, requests-per-second, and average latency over the last 60 seconds. Elapsed and ETA freeze at the final values when a job ends so you can see what the actual run duration was. Updates over a WebSocket at 4 Hz, same cadence as the terminal panel.
* **Run Job tab**: each CLI flag has a clearly labelled form control. Pick snapshot, import, or direct server-to-server transfer; select libraries (or export files), set worker count, toggle verbose and strict match, fill in path remap if needed, then submit. Direct mode adds a Users section with checkboxes for the owner and every managed user that exists on both servers  uncheck anyone you don't want to migrate. Jobs run one at a time; subsequent submissions queue.
* **Servers tab**: register, edit, test, and remove Plex servers by friendly name. The Server Users block under each row lists the owner and every Plex Home managed user; click the owner's display name to edit it inline (the chosen name propagates to the dashboard header, the activity feed, and the direct-transfer user selector). Removing a server is a cascade  schedules referencing it, export files produced by it, and per-run log directories under its slug all get deleted with a confirmation dialog showing the counts.
* **Schedules tab**: create, edit, enable / disable, and delete recurring snapshot schedules. Schedules fire in the container's configured timezone (set the `TZ` env var in `docker-compose.yml`), and the topbar shows a live server-time clock so you always know what time the schedule engine sees. Frequency: hourly, daily, or weekly, at a wall-clock time you choose.
* **Logs tab**: a three-pane browser over `plex_logs/`. Click a run directory, click a file, read the contents in the browser. The viewer has a case-insensitive keyword filter  type any substring and matching lines stay visible with the match highlighted, everything else hides. Files larger than 4 MB show the tail.
* **Exports tab** (under Settings): the snapshot registry, grouped
  by server. Each row is one captured snapshot with its `.db` size,
  library count, user count, and capture timestamp. Download
  streams the snapshot as `.plexexport.json`, generated on demand
  from the `.db` unless a pre-built sidecar exists. Each per-server
  group has a "Clear all snapshots for this server" danger button
  (db_admin gated). A separate "Legacy JSON archives" section lists
  any pre-PR-13 `.plexexport.json` files relocated to
  `snapshots/legacy/` on first boot.
* **Settings tab**: Plex URL, Plex token (write-only, never echoed back to the browser), and the default values for every per-run option. Output and log paths must be container-visible  Windows host paths like `Y:\plexexports` are rejected with a message explaining how to bind-mount external drives in `docker-compose.yml`.

### Stop / restart / inspect

| Action | Command |
|---|---|
| Stop both containers, keep volumes | `docker compose down` |
| Stop containers and wipe volumes (does NOT touch host bind mounts) | `docker compose down -v` |
| Rebuild after a code change | `docker compose up --build` |
| Tail backend logs | `docker compose logs -f backend` |
| Run the CLI inside the backend container | `docker compose exec backend python plexmigrate.py --help` |

### Where data lives

Three host directories are bind-mounted into the backend container so all data outlives the container lifecycle:

| Host path | Container path | Contents |
|---|---|---|
| `./snapshots/` | `/app/snapshots/` | Per-server snapshot `.db` files. `snapshots/legacy/` holds any pre-PR-13 `.plexexport.json` archives moved there by the first-boot migration. |
| `./plex_logs/` | `/app/plex_logs/` | Per-run log directories (same format as CLI mode) |
| `./server_data/` | `/app/server_data/` | `settings.json`, `schedules.json`, `servers.json`, `media.db`, `snapshots.db` (snapshot registry, PR-13), `auth.db` (PR-A1), and the binary `.keyfile` used for at-rest encryption (v0.9.5+) |

You can inspect and edit everything in the table from the host. The JSON files use 2-space indent and are easy to diff. The `.keyfile` is 32 raw bytes  don't open it in a text editor, don't commit it to source control (`.gitignore` already excludes it), and don't delete it unless you're prepared to re-enter every registered server's token.

### Security notes

* The backend has **always-on multi-user authentication as of PR-A2** (previously opt-in via `PLEXMIGRATE_AUTH_ENABLED=true`; that env var has been removed). On first boot the web UI walks you through creating a root admin account; every subsequent boot goes to the login screen. Every API call and the WebSocket require a JWT issued by `/api/auth/login`. **Upgrade note:** on first boot after upgrading to PR-A2, you'll be prompted to log in with your existing admin credentials - there is no fallback flag to disable auth.
* If you want LAN access (so a phone, tablet, or another desktop on your home network can run the UI), expose the frontend port to all interfaces. The next section walks through exactly how. The backend should stay bound to loopback even after that change  the frontend's nginx proxies API and WebSocket traffic to it over Docker's internal bridge network, so no external interface needs to see it directly.
* **Tokens are encrypted at rest as of v0.9.5.** A 256-bit Fernet key is generated on first boot and stored at `server_data/.keyfile` (raw bytes, mode `0o600`). Every Plex token in `servers.json` and the legacy `settings.json` is encrypted with that key; on disk you'll see `gAAAAAB…` ciphertexts rather than the raw tokens, and each row carries an `"_encrypted": true` marker. If the keyfile is deleted or replaced, existing encrypted tokens become unrecoverable  the operator gets an actionable "re-enter credentials" message in the UI rather than a crash. Decryption happens only at the point a token is handed to plexapi; the plaintext never lands in any log line, API response, or export file.
* A log-scrubber filter strips credentials from every record written through Python's logging framework, covering the Plex token (`X-Plex-Token`), the plex.tv login token (`authToken`), the JWT (`access_token` / `?token=`), any `password` field, and Fernet ciphertext blobs, so plexapi exceptions whose message includes a token-bearing URL don't leak it into `runtime.log`, `errors.log`, or Docker's stdout. The job worker's traceback path was rerouted from `traceback.print_exc()` (which bypassed handlers) through `logger.error(..., exc_info=True)` so the scrubber catches it too.
* The Settings and Servers API endpoints return `""` (or `has_token: true/false`) for the token field, never the value itself. The Pydantic models reject Windows host paths in `output_dir` / `log_dir` so a misconfigured run can't silently write into the container's ephemeral filesystem.
* Credential-bearing files under `server_data/` (`settings.json`, `servers.json`, `media.db`, `.keyfile`, `.auth_secret`) are created with mode `0o600`. **On Windows this mode bit is ignored** (`os.chmod` does not produce a restrictive ACL), so on a Windows host the `server_data/` **directory ACL is the actual security boundary** and must be locked down to the service account. Protect `server_data/` the same way you protect any other server credentials directory.

### Exposing PlexMigrate to other devices on your network

Out of the box, PlexMigrate is reachable only from the Docker host. If you want to run the web UI from a **phone, tablet, or another desktop** on your home network  without setting up a full reverse proxy  there's a two-step path: turn on the built-in auth layer, then expose the frontend port. This walkthrough does both.

**Strong recommendation: enable auth FIRST, port-bind SECOND.** Exposing the frontend to your LAN without authentication means anyone on the same network can open the UI, register your Plex servers, dump your watch history, or worse. The auth layer adds a login screen, gates every API call on a JWT, and stays passive when you don't need it. There is no good reason to do this in the opposite order.

**Pros of exposing the UI to your LAN:**

* Submit and watch jobs from any device  useful when a long fan-out is running on your headless server and you'd rather check on it from the couch.
* Operate the UI from a screen larger than the host's (e.g. tablet on a desk while the Docker host is a NUC under the TV).
* Multiple people in the household can have their own logins (use **POST /api/auth/users** from an admin account to create operator-role accounts).

**Cons / things to know:**

* The backend holds Plex auth tokens. Even with the login layer in front, you're exposing more attack surface than the loopback-only default. Don't expose to networks you don't control  guest Wi-Fi, public Wi-Fi, corporate networks, etc.
* JWT secrets and bcrypt password hashes both live under `server_data/`. Anyone with read access to that directory bypasses the login layer entirely. The bind-mounted volume should have host-level permissions matching the trust level of the LAN you're exposing to.
* The backend still has no rate-limiting on the login endpoint. A determined attacker on your LAN with weeks of time could brute-force a short password. Use a strong one (12+ random characters or a four-word passphrase).
* This setup doesn't get you remote access from outside your home  for that, use a VPN (Tailscale, WireGuard) or a real reverse proxy with TLS. Don't port-forward 8080 to the open internet.

**Step 1  no auth setup needed.** As of PR-A2 auth is always on. On first boot the UI walks you through creating a root admin account; subsequent boots show the login screen. Skip directly to Step 2.

The first time you visit the UI after enabling, it walks you through creating an admin account. Make the password strong  12 characters minimum, a memorable passphrase is fine.

**Step 2  expose the frontend port to your LAN.** In the same `docker-compose.yml`, find the `frontend` service's `ports:` block and remove the `127.0.0.1:` prefix:

```yaml
frontend:
  ports:
    - "8080:80"                # was "127.0.0.1:8080:80"
```

Leave the **backend** port alone  keep it as `"127.0.0.1:8000:8000"`. The frontend container talks to the backend over Docker's internal bridge network (service name `backend:8000`), so the host's published backend port is only used for direct local debugging. Keeping it loopback-only means even if you later turn auth off again, the backend stays unreachable from the LAN.

Recreate:

```bash
docker compose up -d
```

`docker compose ps` should now show the frontend's port mapping as `0.0.0.0:8080->80/tcp` (rather than `127.0.0.1:8080->80/tcp`). If it doesn't, your compose file change didn't take  try `docker compose down && docker compose up -d`.

**Step 3  connect from the other device.** Find your Docker host's LAN IP:

| Host OS | Command | What to look for |
|---|---|---|
| Windows | `ipconfig` in PowerShell | `IPv4 Address` under the active Wi-Fi or Ethernet adapter |
| Linux   | `ip addr` | `inet 192.168.x.x` under the active interface |
| macOS   | `ifconfig` or System Settings → Network | The IP under the active adapter |

Visit `http://<that-IP>:8080` in the device's browser. You should see the PlexMigrate login screen. Sign in with the admin you created in Step 1.

**If it doesn't connect:**

* **Firewall.** Windows Defender Firewall almost always prompts the first time Docker Desktop tries to listen on a non-loopback interface. If you missed the prompt, the device's browser will time out. Open `wf.msc` and either temporarily disable the Public profile firewall to confirm the cause, or (better) add a permanent inbound rule allowing TCP 8080 on the private profile only.
* **Subnet isolation.** Some routers separate the main Wi-Fi from a guest or IoT network. Both devices need to be on the same subnet for direct IP access to work.
* **Auth gives 401 forever.** After enabling auth for the first time, if the UI keeps bouncing you back to the login screen, it's almost always one of: the WebSocket close code 4001 path firing because the persisted token is stale (clear browser session storage and log in again), or your time-of-day clock skew is several hours off the host's (rare, but JWT `exp` validation is sensitive to this).
* **Backend wasn't restarted.** Env var changes only take effect on container recreate. `curl http://localhost:8000/api/auth/status` from the host should report `{"auth_enabled": true, ...}`. If it still says `false`, run `docker compose down && docker compose up -d`.

**Reverting:** to lock the UI back to localhost only, change the line back to `"127.0.0.1:8080:80"` and `docker compose up -d`. You can leave auth enabled either way  it's harmless on a single-host install and means you don't have to redo the setup walkthrough if you re-expose later.

---

## Makefile

Two convenience targets at the project root:

| Target | What it does |
|---|---|
| `make docker` | Runs `docker compose up --build`. Builds and starts the full web stack (backend + frontend) on `http://localhost:8080`. |
| `make cli` | Creates `./venv/`, installs every pip dependency (engine + server) into it, prints the activation command. For users who want only the terminal CLI and no Docker. |
| `make clean` | Removes `./venv/`. Does not touch `snapshots/`, `plex_logs/`, or `server_data/`. |
| `make help` (or just `make`) | Prints the target list. |

`make cli` auto-detects `python3` vs `python` on PATH and prints the right activation command for your shell (PowerShell, cmd, or POSIX). You can still install the deps the old way (`pip install plexapi rich requests`) if you don't want the server dependencies. The server deps are only required when you run the FastAPI server.

---

## Multi-Server Support

Starting in v0.9.0, PlexMigrate manages a registry of Plex servers rather than a single connection. Every snapshot, import, and schedule targets a specific registered server by friendly name. A new direct transfer mode moves data from one registered server straight into another in memory.

### Registering servers

#### Web UI

Open the **Servers** tab in the web frontend. Click **+ Add Server**, fill in:

* **Friendly name:** any string. You'll pick this in CLI flags, the Run Job form, and schedules. Names must be unique.
* **Server URL:** full URL including protocol and port (for example `http://host.docker.internal:32400`).
* **Plex authentication token:** same token you'd find via the Plex web UI's `X-Plex-Token` URL param.

When you save, PlexMigrate adds the server to the registry and immediately probes the connection. The probe populates the status indicator and discovers the library catalogue. You can later **Test** the connection, **Edit** the fields, or **Remove** the server from the registry.

> **Removing a server from the registry never deletes any `.plexexport.json` files or log directories produced from that server.** The registry is just a pointer table. The files on disk live independently.

#### CLI

```
python plexmigrate.py --add-server "Plex1" --server http://192.168.1.10:32400 --token YOUR_TOKEN
python plexmigrate.py --list-servers
python plexmigrate.py --test-server "Plex1"
python plexmigrate.py --rename-server "Plex1" "Living Room Plex"
python plexmigrate.py --remove-server "Living Room Plex"
```

The CLI and web UI read and write the same `server_data/servers.json` file, so a server registered from one shows up immediately in the other.

### Targeting registered servers

#### CLI

```
# Snapshot from a registered server
python plexmigrate.py --snapshot --source-server "Plex1" --libraries "Movies,Music"

# Restore into a registered server
python plexmigrate.py --import --dest-server "Plex2" --input-file Movies_Plex1_20260511_015458.plexexport.json

# Direct server-to-server transfer (no intermediate file)
python plexmigrate.py --direct --source-server "Plex1" --dest-server "Plex2" --libraries "Movies"
```

The legacy ad-hoc form `--server URL --token TOK` still works for one-off use without registering a server.

#### Web UI

The **Run Job** tab has an operation selector (Snapshot, Restore, or Direct transfer) and a server selector below it. In direct transfer mode the form shows a side-by-side "Source server → Destination server" picker so the direction of data flow is unambiguous. Library and export-file pickers populate from the selected server.

### Live status indicators (v0.9.1)

The Servers tab and the Run Job server selectors poll each registered Plex server every 30 seconds with a lightweight `/identity` request. The result is a coloured dot next to each server (green for reachable, red for unreachable or auth failure, amber for unknown) and the current response time in milliseconds. The poll is cheap. It doesn't enumerate libraries or fetch metadata, so leaving the web UI open in the background won't generate meaningful API load on your Plex servers.

The Servers tab also has a per-row **Refresh** button that runs the heavier `test_connection` probe and re-enumerates the libraries. The **Add Server** form has its own **Test Connection** button that probes the URL and token before the row can be saved. The Remove button asks for confirmation and reminds you that removing a server doesn't delete any snapshot files or log directories on disk.

### Direct transfer fallback (v0.9.1)

When you start a direct server-to-server transfer, PlexMigrate first tries the in-memory direct path: reading from the source API and writing to the destination API at the same time. If that path fails for any reason for any library (a network blip, an unexpected API response, OOM on a very large library), PlexMigrate automatically falls back to a chained snapshot-then-import for that library:

1. Source data is gathered into a temporary file `<library>_<source>-to-<dest>_<timestamp>.tmp.plexexport.json` written to your configured output directory.
2. That file is immediately imported into the destination via the normal additive merge rules.
3. On successful import, the file is deleted.
4. If anything in step 2 fails, the file stays on disk, clearly marked with the `.tmp` infix, so you can re-import it manually after fixing the underlying issue.

The dashboard activity feed announces the fallback (`Direct path unavailable  falling back to chained.`) so you know the operation has changed paths. The end result for your data is the same either way.

### Per-user transfer scope (v0.9.6+)

When you pick **Direct Transfer** in the Run Job form, after both servers are chosen a new **Users** section appears. It shows three groups computed live from each server's `/accounts` data: users present on both servers (with checkboxes, default-checked), users present only on the source (greyed out, "Not on destination server"), and an informational footer explaining how to invite missing users. The owner appears in the list alongside managed users  uncheck them and the run skips the entire library-level data block (watch history, playlists, library-level collections, ratings) with a clear log line. Personal collections (Plex Pass feature) ride with each included user's block automatically; pre-v0.9.7 these were silently dropped from every snapshot, now they're correctly captured and restored per-user.

### Fan-out transfer (v0.10.0)

In the Run Job form's **Destination Server** picker, you can now pick more than one server. As soon as a second one is checked the panel re-labels itself **Destination Servers (Fan-out)** and a small banner notes how many destinations the job will write to. Submit, and the dashboard auto-switches to the fan-out view: a top strip showing the source and per-state counts, and one card below per destination, each with its own status badge, log directory, and progress bars.

This works for both **Direct Transfer** (one source server feeding many destinations) and **Restore** (one set of `.plexexport.json` files restored into many destinations). The same additive-only merge rules apply per destination  nothing on any destination is ever deleted or reduced. Destinations run in **parallel** on their own threads; a destination that fails takes only its own card down, and siblings keep running. Per-user filtering, library selection, and remap-path are applied to every destination in the job.

For users on the per-user filter: the included intersection is computed across the source AND every destination, so a managed user must exist on all of them to ride along by default. You can still uncheck individuals to exclude them entirely.

Each destination thread sets its own per-run state (active Plex URL, token, owner, dashboard, per-library accumulators) via Python `contextvars.ContextVar`. The engine's existing `submit_with_context` helper, used everywhere it submits work to a `ThreadPoolExecutor`, copies the destination's context into each worker so HTTP calls and per-library logs go to the right destination automatically. Each destination's per-library success/fail logs and `troubleshoot.log` are correctly isolated; `runtime.log`, `errors.log`, and `media.log` are currently still shared across destinations of one fan-out (the engine attaches FileHandlers to named loggers), which a future release will route through a context-aware filter so each destination's log dir contains only its own records.

### Filename and log directory conventions

Log directories and snapshot filenames from v0.9.0 onwards carry the friendly server's slugified name as a prefix, so outputs from different servers never collide:

| Operation | Old (v0.8.0) | New (v0.9.0) |
|---|---|---|
| Snapshot file | `Movies_20260510_135425.plexexport.json` | `Movies_Plex1_20260510_135425.plexexport.json` |
| Log directory | `run_20260510_135425_PASS/` | `run_Plex1_20260510_135425_PASS/` |
| Direct transfer log dir | (didn't exist) | `run_Plex1-to-Plex2_20260510_135425_PASS/` |

### Migration from v0.8.0

On first boot of v0.9.0, the FastAPI server checks for legacy `plex_url`/`plex_token` fields in `server_data/settings.json`. If both are non-empty AND the registry is empty, it registers them as a server named `"Default"` and clears the legacy fields. Your existing setup keeps working without any manual reconfiguration. You can rename the migrated server in one click from the Servers tab. If you already had servers registered and the legacy fields are still set (shouldn't happen, but a defensive check), the legacy fields are quietly cleared with no duplicate row.

**Schedules.** Schedules created in v0.8.0 don't carry a `source_server_name`. The scheduler skips such schedules with one warning per fire and rolls their `next_run_at` forward. Open the **Schedules** tab and edit each one to pick a registered server.

---

## Smart Playlists

Smart playlists are playlists whose contents are generated by a saved filter (for example, "all unwatched Action movies added this year"). PlexMigrate **cannot transfer smart playlists automatically** because the filter query contains server-specific IDs that are different on every Plex installation.

When PlexMigrate encounters a smart playlist, it:
1. Records it in the failure log with the category "Smart Playlist  Requires Manual Recreation."
2. Saves the original filter URL in the run log so you have it for reference.
3. Does not create any placeholder playlist on the target server.

To restore a smart playlist: open Plex on the target server, create a new Smart Playlist, and re-enter the same filter criteria. The run log entry for that playlist shows the original filter URL.

---

## Plex Home Users

If your Plex server is linked to a Plex.tv account and you use Plex Home (multiple user profiles sharing one server), PlexMigrate automatically snapshots and imports each managed user's watch history, playlists, and ratings independently. Each user's data lives in the export file under a `"users"` section and is restored into the correct profile on the target server.

**Requirements for multi-user support:**
- The server must be linked to a Plex.tv account (not using a LocalAdminToken).
- The managed users must exist on the target server with the same usernames before you run the import.

**What happens if a user is on the old server but not the new one yet?** PlexMigrate logs which users it found on the target server and which ones exist in the export before it starts importing, so you can see the gap immediately. Users not found on the target are skipped with an INFO log, not an error. The end-of-import summary lists which users were imported and which were skipped, by name. Re-invite the skipped users to the new server and re-run the import to restore their data.

If the server is not linked to Plex.tv, PlexMigrate logs a note and continues. Only admin account data is processed, with no error.

---

## Terminal Mode (CLI)

> **Heads-up: terminal mode is the legacy path.** The Docker + Web UI is the recommended way to run PlexMigrate and is the mode that gets the most testing. The terminal CLI still ships in every release and still works, but it does not get the same test coverage, so it can drift behind on bugs and usability over time. If you find something broken here, please open an issue and I will do my best to address it.

If you don't want Docker (and the web UI), the original terminal CLI is still here. Everything below this point is for running PlexMigrate from a shell. Docker users can skip to [Step-by-Step: Migrating to a New Server](#step-by-step-migrating-to-a-new-server) or [Common Problems](#common-problems).

### Before You Start

You will need the following before running PlexMigrate from the CLI:

1. **Python 3.9 or newer.** Download from https://python.org/downloads/. During installation on Windows, check "Add Python to PATH."

2. **A Plex Media Token.** A secret key that lets the script talk to your Plex server. To find yours:
   - Open Plex Web in a browser and play any item.
   - Open your browser's developer tools (F12), go to the Network tab.
   - Look for any request to your Plex server and find `X-Plex-Token` in the URL or headers.
   - Alternatively, follow Plex's official guide at: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

3. **Plex Media Server running** and reachable at the URL you will pass to `--server` (default: `http://localhost:32400`). For snapshot, this is your old server. For import, this is your new server. The server doesn't need to be idle. Active streams and playback are not affected.

4. **For import:** at least one `.plexexport.json` file from the snapshot step, and the media files already present in the target Plex library. Plex must have scanned them before you import. Items that don't exist in the library yet can't be matched.

### Installation

If you just want to get going, see [QUICKSTART.md](QUICKSTART.md). The notes below cover the per-OS specifics.

**Windows.** Open Command Prompt or PowerShell, `cd` into the project folder, then:
```
pip install plexapi rich requests
```

**macOS.** Open Terminal, `cd` into the project folder, then:
```
pip3 install plexapi rich requests
```

**Linux (Debian, Ubuntu, Raspberry Pi OS).** A bare `pip install` will fail with `externally-managed-environment`. Use a virtual environment:
```
sudo apt install python3-full python3-venv
python3 -m venv venv
source venv/bin/activate
pip install plexapi rich requests
```

Don't use `sudo` with `python3 -m venv` or `pip install` here. The venv must be owned by your user account. Every new terminal session needs `source venv/bin/activate` before you run the script (or call `venv/bin/python3 plexmigrate.py ...` directly). See "error: externally-managed-environment" under [Common Problems](#common-problems) for the full explanation.

### How to Run

Same commands on every OS. Use `python` on Windows, `python3` on macOS and Linux. On Linux, activate the venv first (`source venv/bin/activate`) or call `venv/bin/python3` directly.

**Snapshot (save your data):**
```
python plexmigrate.py --snapshot --server http://localhost:32400
```

**Restore (restore your data):**
```
python plexmigrate.py --import --server http://localhost:32400 --input-file "Movies_20260509_173300.plexexport.json"
```

**Interactive mode (no flags, the script asks you what to do):**
```
python plexmigrate.py
```

### Flags Reference

| Flag | What it does | Example |
|---|---|---|
| `--snapshot` | Run in snapshot mode (save data from this server) | `--snapshot` |
| `--import` | Run in import mode (restore data to this server) | `--import` |
| `--token TOKEN` | Your Plex authentication token | `--token abc123xyz` |
| `--server URL` | URL of the Plex server to connect to | `--server http://192.168.1.10:32400` |
| `--output-dir PATH` | Where to save snapshot files (default: `./snapshots`) | `--output-dir /mnt/export/plex` |
| `--input-file FILE` | One or more `.plexexport.json` files to import | `--input-file Movies.plexexport.json Music.plexexport.json` |
| `--workers N` | How many parallel worker threads to use | `--workers 8` |
| `--libraries NAMES` | Which libraries to process (skip interactive prompt) | `--libraries "Movies,TV Shows,Music"` |
| `--verbose` | Print and log extra debug information | `--verbose` |
| `--log-dir PATH` | Where to save log files (default: `./plex_logs`) | `--log-dir /var/log/plexmigrate` |
| `--remap-path OLD NEW` | Change the root path for media files during import | `--remap-path /media/plex /mnt/storage` |
| `--no-strict-match` | Allow best-guess when multiple title matches exist (use carefully, may select the wrong item) | `--no-strict-match` |

> **Note:** `--overwrite-playlists` is still accepted for backward compatibility but has no effect. Since v0.2.0 all playlist imports use additive union merge.

The registry and direct-transfer flags (`--add-server`, `--list-servers`, `--source-server`, `--dest-server`, `--direct`, and friends) are documented under [Multi-Server Support](#multi-server-support).

---

## Step-by-Step: Migrating to a New Server

1. **On your old server:** Run the snapshot (Run Job tab in the web UI, or `--snapshot` on the CLI). Select the libraries you want to back up.
2. The script creates `.plexexport.json` files in the `./snapshots/` folder, one per library.
3. **Copy those files** to the machine where your new server runs. USB drive, network share, cloud storage; any method works.
4. **On your new server:** Make sure your media files are accessible and Plex has scanned them. The items must appear in Plex before you can import.
5. Run the import, pointing at the `.plexexport.json` files.
6. Check the `./plex_logs/` folder for a summary and any items that need manual attention.

---

## Log Files

All logs land in `./plex_logs/` (or the path you set with `--log-dir`). Filenames carry a timestamp so runs never overwrite each other.

| Log file | When created | What's in it |
|---|---|---|
| `run_YYYYMMDD_HHMMSS.log` | Always, one per run | Full transcript: startup, library discovery, every action, all successes and failures, final summary. Start here when something goes wrong. Add `--verbose` for DEBUG detail. |
| `{LibraryName}_success_YYYYMMDD_HHMMSS.log` | At least one item in that library succeeded | Every successful item, the matching method (GUID lookup, file path, or title search), and the action taken. Action tags: `[CREATED]`, `[APPENDED]`, `[RATING SET]`, `[SKIPPED  ...]`. Ends with a totals summary and success rate. |
| `{LibraryName}_fail_YYYYMMDD_HHMMSS.log` | At least one item in that library failed | Every failed item, the GUID and file path tried, and the specific reason. Same summary block as the success log. |
| `troubleshoot_YYYYMMDD_HHMMSS.log` | Any failures occurred | Failures grouped by category (file not found, ambiguous title match, etc.) with a plain-English explanation and step-by-step fix for each, plus a "Next Steps" section. |
| `unresolved_YYYYMMDD_HHMMSS.log` | Items failed all matching tiers | One-line-per-item checklist for manual restoration in Plex, with a short intro explaining what to do with the file. |

---

## Common Problems

The troubleshooting catalogue lives in its own file now: **[commonproblems.md](commonproblems.md)**. That's where you'll find the resolutions for everything the README used to cover here  `externally-managed-environment` pip errors, missing modules, dashboard refresh quirks on Linux, the "local:// GUID" music-track edge case, the empty-`guids` IndexError in Play Count import, the playlist 400 bad_request on large static playlists, and so on  plus new entries that don't belong in the README's main flow. The split keeps this file focused on "how to use the tool" while leaving room for the troubleshooting list to grow without making the README itself a 1000-line scroll.

If you hit something that isn't documented in either file, the run log directory under `plex_logs/run_<slug>_<timestamp>_FAIL/` carries the per-library success/fail logs, the runtime/errors/media streams, and a generated `troubleshoot.log` keyed by failure category  that's the first place to look before reporting anything.

---

## Tips for Large Libraries

- Use `--workers 16` or higher on machines with many CPU cores to speed up processing.
- Run the snapshot overnight if your library is very large. The script is safe to leave running.
- After import, check the Plex dashboard to verify watch history appears correctly on a few items before assuming everything is done.
- You can safely run the import more than once. The additive merge logic means repeated runs only add what's still missing. They won't create duplicates.
- While the script runs, a full terminal dashboard shows a thread pool summary, per-library progress bars with ETA, run stats (completed / skipped / failed / unresolved), and a match resolution breakdown (GUID / filepath / suffix / fuzzy). It also shows a live activity feed of the last 8 significant events. On terminals smaller than 80×22, the dashboard falls back to compact Rich progress bars instead.
- **Keyboard shortcuts** while the dashboard is visible: **Q** to quit cleanly, **V** to toggle verbose (DEBUG) console output, **P** to pause or resume all worker threads at safe checkpoints, **L** to open the log folder in your file manager, **S** to open the connected Plex server in your browser (auto-logged in), and **R** to force an immediate dashboard refresh.

### Collection performance settings (v0.12.3)

Collections are one of the heaviest parts of an snapshot or direct transfer when you have many home users. On a server with 12 users and 300 library-wide collections, the naïve approach sends 3,600 API calls just for collections  one `coll.items()` round-trip per collection per user, even though all 300 are visible to everyone. Two settings in the **Engine** section of the Run Job form address this directly.

#### Why collections are expensive per-user

Plex doesn't offer a "give me only this user's personal collections" endpoint. The only way to find a user's personal collections (ones they created themselves that aren't visible to everyone) is to fetch the full collection list from their account, then subtract the library-wide ones. That subtraction is the right behaviour  the problem is that the pre-v0.12.3 code paid the full serialization and items-fetch cost on every collection in the list before the subtraction, so library-wide collections were processed N times (once per user).

Three-layer optimisation now applies automatically on the per-user pass:

1. **Early exit**  if every collection the user can see is already in the library-wide set, the user has no personal collections at all. PlexMigrate returns immediately with no further API calls.
2. **Skip-before-work**  for the remaining users who do have personal collections, library-wide entries are skipped before `coll.items()`, serialization, log writes, or dashboard counter increments fire. Only genuinely personal collections pay the full cost.
3. **Fast owner detection** (opt-in)  see the table below.

| Setting | What it does | When to use it | When to leave it off |
|---|---|---|---|
| **Skip collections** | Omits the collection gather entirely  owner block and all per-user passes. Watch history, playlists, and ratings transfer normally. | You don't need collections at the destination, or the destination server will build its own (e.g. a fresh install that auto-generates franchise collections). Fastest possible transfer. | You have personal Plex Pass collections you want to preserve across servers. |
| **Skip playlists** | Omits the playlist gather entirely  owner block and all per-user passes. Watch history, collections, and ratings transfer normally. | Migrating to a fresh server and you'd rather rebuild playlists by hand, or the bulk of your playlists are smart playlists (which can't transfer automatically and would all appear in the failure log anyway). Also useful when a quick watch-history sync is all you need. | You have regular (non-smart) playlists you want preserved at the destination. |
| **Fast collection detection** | Reads Plex's `librarySectionUserID` attribute on each collection to determine ownership without a set lookup. `None`/`0` = library-wide (skip); any other value = personal (process). Eliminates even the set-membership check for each item. | You are on **Plex Media Server ≥ 1.32** and have a large number of library-wide collections (roughly 100+) and/or many home users (5+). The gains are most visible when early-exit fires for most users but the remaining users still have a large list to iterate. | You are on an older Plex build. The engine falls back to the standard rating-key method automatically if the attribute isn't there, so it is safe to enable, but you gain nothing on old servers. |

**Rule of thumb for large libraries:**
- If you have fewer than 5 home users and/or fewer than 50 collections, the built-in three-layer optimisation is already fast enough  no settings change needed.
- If you have 10+ home users **and** 200+ library-wide collections, enable **Fast collection detection**.
- If you're doing a speed-first migration and will rebuild collections by hand afterward, enable **Skip collections**.
- If most of your playlists are smart playlists, enable **Skip playlists**  they'll all fail anyway and skipping them is honest and faster.
- Never enable both skip options and fast detection together; if you're skipping collections the fast-detection setting has nothing to act on.

---

## Understanding Performance and Threading

All worker threads run on the host (the machine running the container or CLI), **not** on your Plex server. Speed depends on your network to Plex, how fast Plex answers, and how many parallel requests Plex tolerates. The default thread count comes from the host's CPU core count, but PlexMigrate spends almost all its time waiting on Plex, so the host's core count barely matters.

### If it's running slowly

1. **Lower the worker count** with `--workers` (CLI) or the Worker threads field (Run Job tab).
2. **Get on the same LAN as Plex.** Single-digit milliseconds on a LAN, hundreds over the internet.
3. **Check whether Plex is busy** streaming or transcoding. That contention shows up as slow API responses.

### If it's running well and you want it faster

Increase the worker count, but watch the **Failed** count in the dashboard. Failed items mean Plex didn't answer in time or returned an error. If Failed climbs, Plex is overwhelmed; drop workers back down.

### Direct server-to-server transfer

Both servers receive API calls at once, so cut workers further to keep both responsive. The Run Job tab shows a banner reminding you of this when you pick direct transfer mode.

### Recommended starting points

| Scenario | Recommended `--workers` | Notes |
|---|---|---|
| Host and Plex server on the same LAN | The default (`min(32, cpu_count × 4)`) | Network is fast and reliable. Plex itself is the bottleneck, and most home servers handle this comfortably. |
| Host remote from Plex / over the internet | **Start with 8** and increase from there | High network round-trip time means more parallel requests in flight, not all of which can be served before timing out. Lower workers = fewer timeouts. |
| Direct server-to-server transfer | **Half the default** (`min(16, cpu_count × 2)`) and monitor both servers | Both servers feel the load. The lower starting point gives you headroom to increase if both stay healthy. |

### Registering many servers

Every snapshot adds load to whichever server it reads from. Stagger your scheduled windows so two schedules don't fire at the same minute against the same Plex. Large music libraries hit the API hardest, so give those breathing room. The Servers tab shows a banner reminding you of this when you have multiple servers registered.

---

## Learning More

The `SOURCES.md` file in this folder contains links and plain-English explanations for every technology, library, and API used in this project. That includes FastAPI, uvicorn, Pydantic, React, Vite, TypeScript, Nginx, and Docker (added in v0.8.0). If you want to understand how PlexMigrate works under the hood, what python-plexapi is, how MusicBrainz GUIDs work, what ThreadPoolExecutor does, or how Plex's scrobble API works, start there. Each entry explains not just what the technology is, but why it matters for this specific project.

**Project structure.** PlexMigrate is split along two seams: the engine (terminal mode and server mode share it) and the two consumers (the CLI driver and the FastAPI server). `plexmigrate.py` is the CLI entry-point driver, around 290 lines. Engine logic lives under `services/`. The optional web layer lives under `server/` and `frontend/`.

Both `plexmigrate.py` and the `services/` folder need to live in the same directory for the CLI to run. You only need the `server/` and `frontend/` directories (plus `Dockerfile.backend`, `docker-compose.yml`, and `Makefile`) if you want the web UI.

<details>
<summary><strong>Full file layout</strong> (click to expand)</summary>

| File / Directory | What it contains |
|---|---|
| `plexmigrate.py` | CLI entry point. Prompts, argument parsing, `main()`. |
| `services/state.py` | All shared module-level globals and singletons (VERSION, console, thread locks, counters) |
| `services/dashboard.py` | Dashboard UI, keyboard handling, Rich Progress factory |
| `services/logging_ops.py` | Logging setup, result recorders, log file writers |
| `services/auth.py` | Token discovery, server connection, library enumeration, home user fetching |
| `services/resolver.py` | Item serialisation and four-tier matching (GUID → filepath → suffix → fuzzy) |
| `services/snapshotter.py` | Full snapshot pipeline including `run_snapshot()` |
| `services/importer.py` | Full import pipeline including `run_import()` |
| `server/app.py` | FastAPI app: REST routes for settings, libraries, jobs, schedules, logs, snapshots; WebSocket at `/ws/dashboard`. |
| `server/jobs.py` | Single-worker job queue that wraps `run_snapshot` / `run_import`. |
| `server/schedules.py` | Background scheduler thread for recurring snapshots. |
| `server/persistence.py` | Atomic JSON file I/O for `schedules.json` and `settings.json`. |
| `server/ws.py` | 4 Hz WebSocket broadcaster  pushes `DashboardState.to_dashboard_frame()` to every connected browser. |
| `server/log_browser.py` | Read-only browse over `plex_logs/`. |
| `server/snapshot_browser.py` | Read-only browse over `snapshots/`. |
| `server/runtime_patches.py` | Runtime monkey patches that put the engine into headless mode (no edits to `services/` source). |
| `server/models.py` | Pydantic request / response schemas. |
| `frontend/src/App.tsx` | React tab layout + WebSocket subscription. |
| `frontend/src/components/*.tsx` | One file per tab (Dashboard, Run Job, Schedules, Logs, Snapshots, Settings). |
| `frontend/src/api.ts` | Typed REST + WebSocket client. |
| `Dockerfile.backend` | Backend container image. |
| `frontend/Dockerfile` | Two-stage React build + nginx serve. |
| `frontend/nginx.conf` | SPA fallback + `/api` and `/ws` reverse proxy to the backend container. |
| `docker-compose.yml` | Two-service orchestration with host bind mounts. |
| `Makefile` | `make docker` and `make cli` targets. |

</details>
