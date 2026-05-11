# PlexMigrate -WIP
**Version 0.9.2**
A tool that moves your Plex watch history, listening history, playlists, collections, and star ratings between Plex servers, without losing any data. 

> **In a hurry?** See [QUICKSTART.md](QUICKSTART.md) for the 5-minute version. This README is the long reference.

There are two ways to run it. Pick whichever fits how you work:

* **Terminal mode** (the original): `python plexmigrate.py`. Same behaviour as every earlier version. Live `htop`-style dashboard, keyboard shortcuts, no server, no Docker. Jump to [How to Run](#how-to-run).
* **Docker + Web UI** (added in v0.8.0): `docker compose up --build` (or `make docker`). Brings up a FastAPI backend and a React frontend in two containers. The web dashboard shows the same live data as the terminal one. Every CLI flag has a form control. You can save scheduled recurring backups. Jump to [Docker and Web UI](#docker-and-web-ui).

**New in v0.9.0: multi-server support.** PlexMigrate now manages a registry of multiple Plex servers and lets you target each one by friendly name. A new direct server-to-server transfer mode moves data from one registered server straight into another, without writing an intermediate `.plexbackup.json` to disk. Jump to [Multi-Server Support](#multi-server-support).

New in v0.9.1: Every operation now requires explicit server selection no silent defaults. The Run Job form shows a live reachability dot and ping latency next to each server, refreshed every 30 seconds, and lower panels stay greyed out until a selection is made. Direct transfers automatically fall back to a chained export-then-import if the in-memory path fails for any reason; the activity feed announces it when it happens.


---

## What This Does

PlexMigrate works through Plex's built-in API, the same interface your Plex app uses when you hit play, mark something watched, or build a playlist. It doesn't touch your media files, move any data on disk, or require you to stop using Plex while it runs. You can keep watching TV or listening to music on any device while an export or import runs in the background.

There are two steps.

**Export.** Run this on your old server, or before you rebuild. PlexMigrate connects to Plex, reads your watch history, resume positions, star ratings, playlists, and collections, and saves them to a set of `.plexbackup.json` files (one per library). Plex must be running on that machine for this step.

**Import.** Run this on your new or freshly rebuilt server. PlexMigrate reads the backup files and restores everything it can find, matching each item using four methods in order: by its global ID, by its exact file path, by a path-suffix match (for cross-platform migrations, see below), and finally by title. Plex must be running on the target machine for this step, but nothing else needs to stop. Active streams and in-progress playback are not affected.

Between those two steps, the backup files are just files on disk. Copy them however you like (USB drive, network share, cloud storage) and run the import whenever you're ready.

PlexMigrate logs everything it does and attempts produces a plain-English troubleshooting report for anything it couldn't restore automatically.

---

## Data Safety

**PlexMigrate never deletes, overwrites, or reduces any data on your target server.** Every import is strictly additive. It only adds what is missing.

Here is what "additive" means for each data type:

- **Watch history**: If an item on the new server already has a higher view count than the backup, the script leaves it alone. It only adds views when the backup count is strictly higher. Resume positions (where you paused) are only restored if the new server has no saved position for that item.
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

* **Dashboard tab**: the live, browser-side version of the terminal dashboard. Thread pool counts broken down by category, run stats (completed / skipped / failed / unresolved), match resolution stats (GUID / filepath / suffix / fuzzy), per-library progress bars with ETA, and the colour-coded activity feed. Updates over a WebSocket at the same 4 Hz cadence as the terminal panel.
* **Run Job tab**: each CLI flag has a clearly labelled form control. Pick export or import, select libraries (or backup files), set worker count, toggle verbose and strict match, fill in path remap if needed, then submit. Jobs run one at a time. Subsequent submissions queue.
* **Schedules tab**: create, edit, enable / disable, and delete recurring export schedules. Schedules persist across container restarts and fire on a server-side background thread. Frequency: hourly, daily, or weekly, at a wall-clock time you choose.
* **Logs tab**: a three-pane browser over `plex_logs/`. Click a run directory, click a file, read the contents in the browser. Files larger than 4 MB show the tail.
* **Exports tab**: every `.plexbackup.json` in your output directory, with library, export timestamp, size, and a download button.
* **Settings tab**: Plex URL, Plex token (write-only, never echoed back to the browser), and the default values for every per-run option.

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
| `./plex_exports/` | `/app/plex_exports/` | `.plexbackup.json` files |
| `./plex_logs/` | `/app/plex_logs/` | Per-run log directories (same format as CLI mode) |
| `./server_data/` | `/app/server_data/` | `settings.json` + `schedules.json` |

You can inspect and edit everything in the table from the host. The JSON files use 2-space indent and are easy to diff.

### Security notes

* The backend has **no built-in authentication**. It holds your Plex token and trusts whoever can reach `localhost:8000` / `localhost:8080`. Both ports bind to `127.0.0.1` by default, so a fresh `make docker` is reachable only from the host that ran it.
* If you want LAN access, put a reverse proxy with authentication (Caddy, Authelia, etc.) in front of the frontend container. Do not change the port binding to `0.0.0.0` without adding auth.
* PlexMigrate stores the Plex token at rest in `./server_data/settings.json` in plaintext. Protect that directory the same way you protect any other server credentials file.

---

## Makefile

Two convenience targets at the project root:

| Target | What it does |
|---|---|
| `make docker` | Runs `docker compose up --build`. Builds and starts the full web stack (backend + frontend) on `http://localhost:8080`. |
| `make cli` | Creates `./venv/`, installs every pip dependency (engine + server) into it, prints the activation command. For users who want only the terminal CLI and no Docker. |
| `make clean` | Removes `./venv/`. Does not touch `plex_exports/`, `plex_logs/`, or `server_data/`. |
| `make help` (or just `make`) | Prints the target list. |

`make cli` auto-detects `python3` vs `python` on PATH and prints the right activation command for your shell (PowerShell, cmd, or POSIX). You can still install the deps the old way (`pip install plexapi rich requests`) if you don't want the server dependencies. The server deps are only required when you run the FastAPI server.

---

## Multi-Server Support

Starting in v0.9.0, PlexMigrate manages a registry of Plex servers rather than a single connection. Every export, import, and schedule targets a specific registered server by friendly name. A new direct transfer mode moves data from one registered server straight into another in memory.

### Registering servers

#### Web UI

Open the **Servers** tab in the web frontend. Click **+ Add Server**, fill in:

* **Friendly name:** any string. You'll pick this in CLI flags, the Run Job form, and schedules. Names must be unique.
* **Server URL:** full URL including protocol and port (for example `http://host.docker.internal:32400`).
* **Plex authentication token:** same token you'd find via the Plex web UI's `X-Plex-Token` URL param.

When you save, PlexMigrate adds the server to the registry and immediately probes the connection. The probe populates the status indicator and discovers the library catalogue. You can later **Test** the connection, **Edit** the fields, or **Remove** the server from the registry.

> **Removing a server from the registry never deletes any `.plexbackup.json` files or log directories produced from that server.** The registry is just a pointer table. The files on disk live independently.

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
# Export from a registered server
python plexmigrate.py --export --source-server "Plex1" --libraries "Movies,Music"

# Import into a registered server
python plexmigrate.py --import --dest-server "Plex2" --input-file Movies_Plex1_20260511_015458.plexbackup.json

# Direct server-to-server transfer (no intermediate file)
python plexmigrate.py --direct --source-server "Plex1" --dest-server "Plex2" --libraries "Movies"
```

The legacy ad-hoc form `--server URL --token TOK` still works for one-off use without registering a server.

#### Web UI

The **Run Job** tab has an operation selector (Export, Import, or Direct transfer) and a server selector below it. In direct transfer mode the form shows a side-by-side "Source server → Destination server" picker so the direction of data flow is unambiguous. Library and backup-file pickers populate from the selected server.

### Live status indicators (v0.9.1)

The Servers tab and the Run Job server selectors poll each registered Plex server every 30 seconds with a lightweight `/identity` request. The result is a coloured dot next to each server (green for reachable, red for unreachable or auth failure, amber for unknown) and the current response time in milliseconds. The poll is cheap. It doesn't enumerate libraries or fetch metadata, so leaving the web UI open in the background won't generate meaningful API load on your Plex servers.

The Servers tab also has a per-row **Refresh** button that runs the heavier `test_connection` probe and re-enumerates the libraries. The **Add Server** form has its own **Test Connection** button that probes the URL and token before the row can be saved. The Remove button asks for confirmation and reminds you that removing a server doesn't delete any export files or log directories on disk.

### Direct transfer fallback (v0.9.1)

When you start a direct server-to-server transfer, PlexMigrate first tries the in-memory direct path: reading from the source API and writing to the destination API at the same time. If that path fails for any reason for any library (a network blip, an unexpected API response, OOM on a very large library), PlexMigrate automatically falls back to a chained export-then-import for that library:

1. Source data is gathered into a temporary file `<library>_<source>-to-<dest>_<timestamp>.tmp.plexbackup.json` written to your configured output directory.
2. That file is immediately imported into the destination via the normal additive merge rules.
3. On successful import, the file is deleted.
4. If anything in step 2 fails, the file stays on disk, clearly marked with the `.tmp` infix, so you can re-import it manually after fixing the underlying issue.

The dashboard activity feed announces the fallback (`Direct path unavailable — falling back to chained.`) so you know the operation has changed paths. The end result for your data is the same either way.

### Filename and log directory conventions

Log directories and export filenames from v0.9.0 onwards carry the friendly server's slugified name as a prefix, so outputs from different servers never collide:

| Operation | Old (v0.8.0) | New (v0.9.0) |
|---|---|---|
| Export file | `Movies_20260510_135425.plexbackup.json` | `Movies_Plex1_20260510_135425.plexbackup.json` |
| Log directory | `run_20260510_135425_PASS/` | `run_Plex1_20260510_135425_PASS/` |
| Direct transfer log dir | (didn't exist) | `run_Plex1-to-Plex2_20260510_135425_PASS/` |

### Migration from v0.8.0

On first boot of v0.9.0, the FastAPI server checks for legacy `plex_url`/`plex_token` fields in `server_data/settings.json`. If both are non-empty AND the registry is empty, it registers them as a server named `"Default"` and clears the legacy fields. Your existing setup keeps working without any manual reconfiguration. You can rename the migrated server in one click from the Servers tab. If you already had servers registered and the legacy fields are still set (shouldn't happen, but a defensive check), the legacy fields are quietly cleared with no duplicate row.

**Schedules.** Schedules created in v0.8.0 don't carry a `source_server_name`. The scheduler skips such schedules with one warning per fire and rolls their `next_run_at` forward. Open the **Schedules** tab and edit each one to pick a registered server.

---

## Smart Playlists

Smart playlists are playlists whose contents are generated by a saved filter (for example, "all unwatched Action movies added this year"). PlexMigrate **cannot transfer smart playlists automatically** because the filter query contains server-specific IDs that are different on every Plex installation.

When PlexMigrate encounters a smart playlist, it:
1. Records it in the failure log with the category "Smart Playlist — Requires Manual Recreation."
2. Saves the original filter URL in the run log so you have it for reference.
3. Does not create any placeholder playlist on the target server.

To restore a smart playlist: open Plex on the target server, create a new Smart Playlist, and re-enter the same filter criteria. The run log entry for that playlist shows the original filter URL.

---

## Plex Home Users

If your Plex server is linked to a Plex.tv account and you use Plex Home (multiple user profiles sharing one server), PlexMigrate automatically exports and imports each managed user's watch history, playlists, and ratings independently. Each user's data lives in the backup file under a `"users"` section and is restored into the correct profile on the target server.

**Requirements for multi-user support:**
- The server must be linked to a Plex.tv account (not using a LocalAdminToken).
- The managed users must exist on the target server with the same usernames before you run the import.

**What happens if a user is on the old server but not the new one yet?** PlexMigrate logs which users it found on the target server and which ones exist in the backup before it starts importing, so you can see the gap immediately. Users not found on the target are skipped with an INFO log, not an error. The end-of-import summary lists which users were imported and which were skipped, by name. Re-invite the skipped users to the new server and re-run the import to restore their data.

If the server is not linked to Plex.tv, PlexMigrate logs a note and continues. Only admin account data is processed, with no error.

---

## Terminal Mode (CLI)

If you don't want Docker (and the web UI), the original terminal CLI is still here and behaves exactly as it did before. Everything below this point is for running PlexMigrate from a shell. Docker users can skip to [Step-by-Step: Migrating to a New Server](#step-by-step-migrating-to-a-new-server) or [Common Problems](#common-problems).

### Before You Start

You will need the following before running PlexMigrate from the CLI:

1. **Python 3.9 or newer.** Download from https://python.org/downloads/. During installation on Windows, check "Add Python to PATH."

2. **A Plex Media Token.** A secret key that lets the script talk to your Plex server. To find yours:
   - Open Plex Web in a browser and play any item.
   - Open your browser's developer tools (F12), go to the Network tab.
   - Look for any request to your Plex server and find `X-Plex-Token` in the URL or headers.
   - Alternatively, follow Plex's official guide at: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

3. **Plex Media Server running** and reachable at the URL you will pass to `--server` (default: `http://localhost:32400`). For export, this is your old server. For import, this is your new server. The server doesn't need to be idle. Active streams and playback are not affected.

4. **For import:** at least one `.plexbackup.json` file from the export step, and the media files already present in the target Plex library. Plex must have scanned them before you import. Items that don't exist in the library yet can't be matched.

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

**Export (save your data):**
```
python plexmigrate.py --export --server http://localhost:32400
```

**Import (restore your data):**
```
python plexmigrate.py --import --server http://localhost:32400 --input-file "Movies_20260509_173300.plexbackup.json"
```

**Interactive mode (no flags, the script asks you what to do):**
```
python plexmigrate.py
```

### Flags Reference

| Flag | What it does | Example |
|---|---|---|
| `--export` | Run in export mode (save data from this server) | `--export` |
| `--import` | Run in import mode (restore data to this server) | `--import` |
| `--token TOKEN` | Your Plex authentication token | `--token abc123xyz` |
| `--server URL` | URL of the Plex server to connect to | `--server http://192.168.1.10:32400` |
| `--output-dir PATH` | Where to save export files (default: `./plex_exports`) | `--output-dir /mnt/backup/plex` |
| `--input-file FILE` | One or more `.plexbackup.json` files to import | `--input-file Movies.plexbackup.json Music.plexbackup.json` |
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

1. **On your old server:** Run the export (Run Job tab in the web UI, or `--export` on the CLI). Select the libraries you want to back up.
2. The script creates `.plexbackup.json` files in the `./plex_exports/` folder, one per library.
3. **Copy those files** to the machine where your new server runs. USB drive, network share, cloud storage; any method works.
4. **On your new server:** Make sure your media files are accessible and Plex has scanned them. The items must appear in Plex before you can import.
5. Run the import, pointing at the `.plexbackup.json` files.
6. Check the `./plex_logs/` folder for a summary and any items that need manual attention.

---

## Log Files

All logs land in `./plex_logs/` (or the path you set with `--log-dir`). Filenames carry a timestamp so runs never overwrite each other.

| Log file | When created | What's in it |
|---|---|---|
| `run_YYYYMMDD_HHMMSS.log` | Always, one per run | Full transcript: startup, library discovery, every action, all successes and failures, final summary. Start here when something goes wrong. Add `--verbose` for DEBUG detail. |
| `{LibraryName}_success_YYYYMMDD_HHMMSS.log` | At least one item in that library succeeded | Every successful item, the matching method (GUID lookup, file path, or title search), and the action taken. Action tags: `[CREATED]`, `[APPENDED]`, `[RATING SET]`, `[SKIPPED — ...]`. Ends with a totals summary and success rate. |
| `{LibraryName}_fail_YYYYMMDD_HHMMSS.log` | At least one item in that library failed | Every failed item, the GUID and file path tried, and the specific reason. Same summary block as the success log. |
| `troubleshoot_YYYYMMDD_HHMMSS.log` | Any failures occurred | Failures grouped by category (file not found, ambiguous title match, etc.) with a plain-English explanation and step-by-step fix for each, plus a "Next Steps" section. |
| `unresolved_YYYYMMDD_HHMMSS.log` | Items failed all matching tiers | One-line-per-item checklist for manual restoration in Plex, with a short intro explaining what to do with the file. |

---

## Common Problems

### "error: externally-managed-environment" when running pip install (Linux)

Modern Debian and Ubuntu systems block system-wide `pip install` to protect the OS Python. It's deliberate, not a bug.

**Fix:** Use a virtual environment. See the Linux section of "Installation" above for the full step-by-step. The short version:
```
sudo apt install python3-full python3-venv
python3 -m venv venv
source venv/bin/activate
pip install plexapi rich requests
```
After that, always run `source venv/bin/activate` in the same terminal before running the script, or use `venv/bin/python3 plexmigrate.py` directly.

---

### "Permission denied" when pip installs into the venv (Linux)

The full error looks like:
```
ERROR: Could not install packages due to an OSError: [Errno 13] Permission denied: '.../venv/bin/plexapi'
```

The `venv/` folder is owned by root (or another user), not by you. Either it was created with `sudo python3 -m venv venv`, or the folder itself has wrong ownership.

**Fix:** Delete the existing venv and recreate it without `sudo`:
```
deactivate
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install plexapi rich requests
```

Never use `sudo` with `python3 -m venv` or `pip install` when working with a virtual environment. A venv lives in your home directory and you own it entirely; that's the whole point.

---

### "No module named 'plexapi'" (or rich, requests)

The packages aren't installed in the Python environment the script runs under. Usually a Linux problem: you installed the packages in one environment (or system-wide), but the script runs with a different Python.

**Fix:**
1. Make sure your virtual environment is active: `source venv/bin/activate`
2. Verify the packages are installed: `pip show plexapi`
3. If missing, install them: `pip install plexapi rich requests`

---

### "Token not found" or "Authentication failed"

The script tried to read your token from Plex's `Preferences.xml` file and either couldn't find it, or the token has expired.

**Fix:**
1. Find your Plex token manually (see "Before You Start" above).
2. Pass it directly: `python plexmigrate.py --token YOUR_TOKEN_HERE`

---

### "No libraries detected" or the library list is empty

The script connected to Plex but found no libraries, or the server returned an empty list.

**Fix:**
1. Make sure Plex Media Server is running on the machine where you're running the script.
2. Check that `http://localhost:32400/web` opens in your browser. If it doesn't, Plex isn't running.
3. If your Plex server is on a different machine, use `--server http://THAT_MACHINE_IP:32400`.
4. Verify your token has admin access to the server.

---

### Migrating between Windows and Linux (or vice versa)

When you export from a Windows Plex server and import on Linux, the stored file paths use Windows roots (`C:\Media\...`) and backslashes, while your Linux server sees `/mnt/plex/...` with forward slashes. PlexMigrate handles this automatically through **suffix path matching**. It strips the root prefix from both the stored path and every item on the target server, normalises separators and case, and compares the last two or three directory components. If they match unambiguously, the item resolves. No configuration needed.

The same trick works in the reverse direction (Linux to Windows), and handles any migration where the root mount point changed but the folder hierarchy beneath it stayed the same.

If the folder structure changed in addition to the root (for example, you reorganised your media tree during the move), suffix matching won't find those items. In that case use `--remap-path OLD NEW` to translate the root prefix:

```
python plexmigrate.py --import --remap-path "A:\" /mnt/music/
```

> **PowerShell note:** when specifying a Windows drive root as the old path, wrap it in single or double quotes so the trailing backslash is not consumed as an escape character. Use `'A:\'` or `"A:\"`. Unquoted `A:\\` passes two backslashes and will not match stored single-backslash paths.

After the root prefix is swapped, any remaining backslashes in the path body are converted to forward slashes. A stored path like `A:\Music\Artist\Album\track.flac` correctly becomes `/mnt/music/Music/Artist/Album/track.flac`.

Remap and suffix matching together cover most reorganisation scenarios. Use suffix matching first (no flags needed), and add `--remap-path` only if suffix matching misses items that clearly exist on the target server.

---

### "Items not matched on import", many items in the failure log

Usually the item exists on the new server but the script couldn't link the stored record to it.

**Fix:**
1. Make sure the item is in the Plex library on the new server. Plex must have scanned it first.
2. If you're migrating between operating systems (Windows to Linux or vice versa), suffix matching runs automatically. Check whether those items appear with a `[FILEPATH-SUFFIX]` tag in the success log.
3. If only the root path changed, use `--remap-path /old/root /new/root`.
4. Open the troubleshooting log for detailed fix steps grouped by failure type.

---

### Many music tracks appear in the unresolved log with "local:// GUID"

Those tracks were never matched to MusicBrainz on the old server, so they have no universal identifier. The script can't reliably find them without one.

**How to fix this before re-exporting:**

1. Open Plex on the **old server** and go to your Music library.
2. Find an album with unmatched tracks. You can see these in the unresolved log.
3. Right-click the album and choose **"Fix Match"** from the menu.
4. Search for the correct album name and select it from the MusicBrainz results.
5. Wait for Plex to finish matching. This may take a minute or two per album.
6. Repeat for all unmatched albums.
7. Once all albums are matched, **re-run the export**. The tracks will now have stable `plex://` identifiers and will match correctly on import.

If you have a large library with many unmatched albums, Plex's "Fix Incorrect Match" and "Fix Match" tools can do this in bulk. Aim to make every album show artist and album art correctly in Plex. That's the signal Plex has matched it.

---

### The script generated log files I don't recognise

Two log files only appear when there's something to report:

* `troubleshoot_YYYYMMDD_HHMMSS.log` is written whenever any failures occurred. It groups failures by category and gives a numbered fix for each.
* `unresolved_YYYYMMDD_HHMMSS.log` is written when items failed every matching tier (GUID, file path, suffix, title). It's a one-line-per-item checklist for manual restoration in Plex.

After fixing the underlying issues, re-run the import. Already-successful items won't be duplicated because the import is always additive.

---

### Expected entries in the logs

A couple of log entries look alarming but are correct behaviour, not problems:

* **`[SKIPPED — already in playlist]`** entries in the success log mean the item was already in the playlist on the target server before the import ran. The script detected the duplicate and skipped it. No action needed.
* **Smart playlists appearing in the failure log.** Smart playlists can't be transferred automatically (see the [Smart Playlists](#smart-playlists) section above for how to recreate them by hand). The failure log entry includes the original filter URL from the source server for reference.

---

### The dashboard reprints itself repeatedly instead of updating in place (Linux)

On Linux, the dashboard panel scrolls down the screen continuously instead of staying pinned at the bottom. The cause is the terminal's `$TERM` variable: it isn't set to a value Rich recognises as supporting full ANSI cursor control. Common in SSH sessions, tmux, screen, and some terminal emulators.

**Check what your terminal reports:**
```
echo $TERM
```
If the output is anything other than `xterm-256color` (for example `screen`, `tmux-256color`, `dumb`, or blank), that's the cause.

**Permanent fix (recommended).**
Add the correct setting to your shell profile so every session has it automatically:
```
echo 'export TERM=xterm-256color' >> ~/.bashrc
source ~/.bashrc
```

**One-time fix (for a single run).**
Prefix the script command with the variable:
```
TERM=xterm-256color python3 plexmigrate.py --export --server http://localhost:32400
```

If you are inside tmux, run `export TERM=xterm-256color` in the tmux pane before running the script, or add `set -g default-terminal "xterm-256color"` to your `~/.tmux.conf` for a permanent fix.

---

### Home user data was not imported

Check the import run log first. PlexMigrate logs two lines at the start of every import run:

```
Target server home users available for import (N): [name1, name2, ...]
Backup contains data for N user(s): [name1, name2, ...]
```

Compare the two lists. Any name in the backup list that is missing from the target list will be skipped. At the end of the run you will also see:

```
Home user import — N skipped (not on target server): [name1, ...]
Add them to Plex Home and re-run to import their data.
```

**Common causes:**
1. The managed user was not yet invited to the target server. Go to Plex Settings → Manage → Users & Sharing and invite them, then re-run the import.
2. The target server is not linked to a Plex.tv account (LocalAdminToken). Multi-user import requires a Plex.tv-linked server.
3. The username on the new server differs from the backup. Username matching is exact and case-sensitive, so the display name must match exactly.

---

## Tips for Large Libraries

- Use `--workers 16` or higher on machines with many CPU cores to speed up processing.
- Run the export overnight if your library is very large. The script is safe to leave running.
- After import, check the Plex dashboard to verify watch history appears correctly on a few items before assuming everything is done.
- You can safely run the import more than once. The additive merge logic means repeated runs only add what's still missing. They won't create duplicates.
- While the script runs, a full terminal dashboard shows a thread pool summary, per-library progress bars with ETA, run stats (completed / skipped / failed / unresolved), and a match resolution breakdown (GUID / filepath / suffix / fuzzy). It also shows a live activity feed of the last 8 significant events. On terminals smaller than 80×22, the dashboard falls back to compact Rich progress bars instead.
- **Keyboard shortcuts** while the dashboard is visible: **Q** to quit cleanly, **V** to toggle verbose (DEBUG) console output, **P** to pause or resume all worker threads at safe checkpoints, **L** to open the log folder in your file manager, **S** to open the connected Plex server in your browser (auto-logged in), and **R** to force an immediate dashboard refresh.

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

Every export adds load to whichever server it reads from. Stagger your scheduled windows so two schedules don't fire at the same minute against the same Plex. Large music libraries hit the API hardest, so give those breathing room. The Servers tab shows a banner reminding you of this when you have multiple servers registered.

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
| `services/exporter.py` | Full export pipeline including `run_export()` |
| `services/importer.py` | Full import pipeline including `run_import()` |
| `server/app.py` | FastAPI app: REST routes for settings, libraries, jobs, schedules, logs, exports; WebSocket at `/ws/dashboard`. |
| `server/jobs.py` | Single-worker job queue that wraps `run_export` / `run_import`. |
| `server/schedules.py` | Background scheduler thread for recurring exports. |
| `server/persistence.py` | Atomic JSON file I/O for `schedules.json` and `settings.json`. |
| `server/ws.py` | 4 Hz WebSocket broadcaster — pushes `DashboardState.snapshot()` to every connected browser. |
| `server/log_browser.py` | Read-only browse over `plex_logs/`. |
| `server/export_browser.py` | Read-only browse over `plex_exports/`. |
| `server/runtime_patches.py` | Runtime monkey patches that put the engine into headless mode (no edits to `services/` source). |
| `server/models.py` | Pydantic request / response schemas. |
| `frontend/src/App.tsx` | React tab layout + WebSocket subscription. |
| `frontend/src/components/*.tsx` | One file per tab (Dashboard, Run Job, Schedules, Logs, Exports, Settings). |
| `frontend/src/api.ts` | Typed REST + WebSocket client. |
| `Dockerfile.backend` | Backend container image. |
| `frontend/Dockerfile` | Two-stage React build + nginx serve. |
| `frontend/nginx.conf` | SPA fallback + `/api` and `/ws` reverse proxy to the backend container. |
| `docker-compose.yml` | Two-service orchestration with host bind mounts. |
| `Makefile` | `make docker` and `make cli` targets. |

</details>
