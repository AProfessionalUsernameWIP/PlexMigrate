# Hestia-MediaManager - Quick Start

A Docker tool to back up and restore your Plex user experience across servers. Preserves watch history, playlists, ratings, and collections via the Plex API. No downtime, no database access. Tested on Windows and Ubuntu migrations in both directions.

***

## Requirements

Docker Desktop (macOS / Windows) or Docker Engine + Compose v2 (Linux). No Python or Node needed on the host.

***

## 1. Bring up the stack

```bash
docker compose up --build
```

This builds and starts two containers: a FastAPI backend on `localhost:8000` (loopback-only, holds your Plex token) and an nginx frontend on `localhost:8080` (the web UI). First boot can take a minute or two while it pulls images and builds.

> You don't need to run this on the same machine as Plex. Any machine on the same network works — just point Hestia at your Plex server's IP later.

## 2. First-boot setup (one time only)

Open **http://localhost:8080** in your browser. On a fresh install you'll see a **Setup** page, not a login page. This is the always-on auth wizard — Hestia requires authentication for every API call, so you have to create an admin account before you can use anything.

1. Pick a username and password for your root admin account.
2. Click **Create root admin**.
3. You're redirected to the login page. Sign in with the credentials you just created.

After this first-boot step, every subsequent visit goes straight to the login page. Tokens are 24-hour-lived; close the tab to log out.

## 3. Add your first Plex server

Once logged in, you'll land on the Dashboard. Add your Plex server before you can snapshot anything:

1. Click the **Servers** tab in the top nav.
2. Click **+ Add Server**.
3. Pick the backend type — **Plex**, **Jellyfin**, or **Emby**.
4. Enter the server URL (e.g. `http://192.168.1.100:32400` for a typical Plex on your LAN) and the access token. For Plex, see [the Plex token guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) if you don't know yours.
5. Click **Save**. The backend probes the server, fetches the library list, and the row appears in the Servers tab with a green status pill if everything's good.

Add as many servers as you need. The same flow works for Jellyfin (use an API key) and Emby.

## 4. Take a snapshot

1. Click the **Jobs** tab.
2. The mode pill defaults to **Snapshot** (the others are Restore and Direct).
3. Pick a source server from the dropdown.
4. Pick which libraries to capture (Movies, TV Shows, Music, etc).
5. Click **Submit job**.
6. The Dashboard takes over with live items/sec, ETR (estimated time remaining), and per-library progress. When it finishes, the snapshot lands in `./snapshots/` on the host and shows up in the Recent Runtimes table.

## 5. Restore to a different server (or the same one)

1. Add the destination server using the same flow as step 3.
2. Click the **Jobs** tab.
3. Switch the mode pill to **Restore**.
4. Pick the source snapshot from the list (or upload a `.db` file if you've moved it from another machine).
5. Pick the destination server.
6. Choose merge mode: **Merge / Higher** (take whichever viewCount is higher, safest), **Merge / Sum** (add the counts together), or **Replace** (overwrite the destination — requires typing REPLACE to confirm, and optionally auto-captures a safety snapshot of the destination first).
7. Click **Submit job**.

***

## Docker commands

| Command | What it does |
|---|---|
| `docker compose up --build` | Build and start everything |
| `docker compose up -d` | Same, detached (run in background) |
| `docker compose down` | Stop containers, keep your data |
| `docker compose logs -f backend` | Tail backend logs in real time |
| `docker compose restart backend` | Restart just the backend after a code change |

***

## Where your data lives

Three host-bind mounts let your data outlive `docker compose down`:

| Host path | Container path | Contents |
|---|---|---|
| `./snapshots` | `/app/snapshots` | Snapshot `.db` files + `.plexexport.json` sidecars |
| `./plex_logs` | `/app/plex_logs` | Per-job runtime logs and error logs |
| `./server_data` | `/app/server_data` | `settings.json`, `schedules.json`, `auth.db`, and the encrypted token vault |

Back up `./server_data` if you want to preserve your registered servers + credentials. Snapshots can always be regenerated; the registry can't.

***

## Next steps

Once you've taken and restored a snapshot successfully, the README has the full operator manual — direct transfer (skips the intermediate file), library mapping between renamed libraries, scheduled snapshots, fan-out to multiple destinations, the Dev Console, and the rest.
