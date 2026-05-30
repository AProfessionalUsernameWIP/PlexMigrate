# Hestia Media Manager - Quick Start

A Docker tool to back up and restore your Plex / Jellyfin / Emby user experience across servers. Preserves watch history, listening history, playlists, ratings, and collections via each backend's native API. No downtime, no database access. Tested on Windows and Ubuntu migrations in both directions.

***

## Requirements

Docker Desktop (macOS / Windows) or Docker Engine + Compose v2 (Linux). No Python or Node needed on the host. Your media server should be reachable from the backend container; the default Plex URL `http://host.docker.internal:32400` is mapped to the host gateway in `docker-compose.yml`, so it works on Linux too.

***

## 1. Bring up the stack

```bash
docker compose up --build
```

This builds and starts two containers: a FastAPI backend on `localhost:8000` (loopback-only, holds your encrypted credentials) and an nginx frontend on `localhost:8080` (the web UI). First boot can take a minute or two while it pulls images and builds.

> You don't need to run this on the same machine as your media server. Any machine on the same network works; just point Hestia at the server's IP later.

## 2. First-boot setup (one time only)

Open **http://localhost:8080** in your browser. On a fresh install you'll see a **Setup** page, not a login page. This is the always-on auth wizard. Hestia requires authentication for every API call, so you have to create an admin account before you can use anything.

1. Pick a username and password for your Root Admin account.
2. Click **Create root admin**.
3. You're redirected to the login page. Sign in with the credentials you just created.

After this first-boot step, every subsequent visit goes straight to the login page. Tokens are 24-hour-lived; close the tab to log out.

## 3. Add your first media server

Once logged in, you'll land on the Dashboard. Add your media server before you can snapshot anything:

1. Click the **Servers** tab in the top nav.
2. Click **+ Add Server**.
3. Pick the backend type: **Plex**, **Jellyfin**, or **Emby**.
4. Enter the server URL (e.g. `http://192.168.1.100:32400` for a typical Plex on your LAN) and the access token (Plex) or API key (Jellyfin / Emby). For Plex, see [the Plex token guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) if you don't know yours. For Jellyfin / Emby, generate the API key from the server's admin dashboard.
5. Click **Save**. The backend probes the server, fetches the library list, and the row appears in the Servers tab with a green status pill if everything's good.

Add as many servers as you need.

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
6. Choose merge mode: **Merge / Higher** (take whichever viewCount is higher, safest), **Merge / Sum** (add the counts together), or **Replace** (overwrite the destination; requires typing REPLACE to confirm, and optionally auto-captures a safety snapshot of the destination first).
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
| `./snapshots` | `/app/snapshots` | Per-server snapshot `.db` files (plus `.plexexport.json` sidecars where present) |
| `./plex_logs` | `/app/plex_logs` | Per-job runtime logs and error logs |
| `./server_data` | `/app/server_data` | `settings.json`, `schedules.json`, `servers.json`, `media.db`, `snapshots.db`, `auth.db`, and the binary `.keyfile` used for at-rest encryption |

Back up `./server_data` if you want to preserve your registered servers + credentials. Snapshots can always be regenerated; the registry can't. The `.keyfile` is 32 raw bytes: do not open it in a text editor, do not commit it to source control (`.gitignore` already excludes it), and do not delete it unless you are prepared to re-enter every registered server's token.

***

## Next steps

Once you've taken and restored a snapshot successfully, the [README](README.md) has the full operator manual. The topics most end users want to know about:

* **[Data Safety](README.md#data-safety)**: the three restore modes (Merge / Higher, Merge / Sum, Replace) and exactly what each one touches.
* **[Identity Mapping and User Impersonation](README.md#identity-mapping-and-user-impersonation)**: how Hestia identifies the same user across servers (the `app_user_uuid`), the 5-step resolution chain, and the cross-platform preflight modal.
* **[Tombstoning](README.md#tombstoning-skipping-users-you-do-not-want-hestia-to-touch)**: skip users you no longer want Hestia to manage from the Servers tab's User Management subsection, with both manual and auto-tombstoning options.
* **[The Translation Problem](README.md#the-translation-problem)**: how Plex's numeric rating maps to Jellyfin / Emby's favorite flag (and back), and the two tunables (`favorite_threshold`, `favorite_as_rating_value`) that let you customize the translation.
* **[Plex Home Users](README.md#plex-home-users-operator-faq)**: the PIN preflight and missing-user gap report you will see when restoring multi-user Plex servers.

Beyond those: direct transfer (skips the intermediate file), library mapping between renamed libraries, scheduled snapshots, fan-out to multiple destinations, the Dev Console, and more.
