# PlexMigrate
A Docker tool to back up and restore your Plex user experience across servers. Preserves watch history, playlists, ratings, and collections via the Plex API no downtime, no database access. Web UI or CLI. Tested on Windows and Ubuntu migrations in both directions.
# PlexMigrate — Quick Start

Back up and restore your Plex watch history, playlists, ratings, and collections between servers. No downtime required. Your server stays live the entire time.

***

## Docker (Web UI)

**Requirements:** Docker Desktop (macOS/Windows) or Docker Engine + Compose v2 (Linux). No Python or Node needed on the host.

```bash
docker compose up --build
```

Open **http://localhost:8080** in your browser, then go to the **Settings** tab and enter your Plex server URL and token.

> You don't need to run this on the same machine as Plex. Any machine on the same network works — just point it at your Plex server's IP address.

### Docker Commands

| Command | What it does |
|---|---|
| `docker compose up --build` | Build and start everything |
| `docker compose down` | Stop containers, keep data |
| `docker compose logs -f backend` | Tail backend logs |
| `docker compose exec backend python plexmigrate.py --help` | Run CLI inside the container |

***

## CLI

**Requirements:** Python 3.9+ and your Plex token.
Find your token here: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

**Install dependencies:**

```bash
pip install plexapi rich requests
```

> **Linux users:** Use a virtual environment to avoid the `externally-managed-environment` error:
> ```bash
> python3 -m venv venv && source venv/bin/activate
> pip install plexapi rich requests
> ```

**Export (back up your data):**

```bash
python plexmigrate.py --export --server http://localhost:32400
```

**Import (restore your data):**

```bash
python plexmigrate.py --import --server http://localhost:32400 --input-file Movies_20260509_173300.plexbackup.json
```

**Interactive mode (no flags — the script walks you through it):**

```bash
python plexmigrate.py
```

***

## CLI Flags

| Flag | What it does | Example |
|---|---|---|
| `--export` | Save data from this server | `--export` |
| `--import` | Restore data to this server | `--import` |
| `--token TOKEN` | Your Plex authentication token | `--token abc123xyz` |
| `--server URL` | Plex server URL | `--server http://192.168.1.10:32400` |
| `--output-dir PATH` | Where to save export files (default: `./plex_exports`) | `--output-dir /mnt/backup` |
| `--input-file FILE` | `.plexbackup.json` file(s) to import | `--input-file Movies.plexbackup.json` |
| `--workers N` | Number of parallel worker threads | `--workers 8` |
| `--libraries NAMES` | Libraries to process, skips the interactive prompt | `--libraries "Movies,TV Shows,Music"` |
| `--verbose` | Extra debug output | `--verbose` |
| `--log-dir PATH` | Where to save logs (default: `./plex_logs`) | `--log-dir /var/log/plexmigrate` |
| `--remap-path OLD NEW` | Translate media root path on import (cross-OS migrations) | `--remap-path /media/plex /mnt/storage` |
| `--no-strict-match` | Allow best-guess when multiple title matches exist | `--no-strict-match` |

### Multi-Server Flags

| Flag | What it does | Example |
|---|---|---|
| `--add-server NAME` | Register a server by friendly name | `--add-server "Plex1" --server http://192.168.1.10:32400 --token TOKEN` |
| `--list-servers` | List all registered servers | `--list-servers` |
| `--test-server NAME` | Test connection to a registered server | `--test-server "Plex1"` |
| `--remove-server NAME` | Remove a server from the registry | `--remove-server "Plex1"` |
| `--source-server NAME` | Source server for export or direct transfer | `--source-server "Plex1"` |
| `--dest-server NAME` | Destination server for import or direct transfer | `--dest-server "Plex2"` |
| `--direct` | Direct server-to-server transfer, no intermediate file | `--direct --source-server "Plex1" --dest-server "Plex2"` |
