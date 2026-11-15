# Video Upload Web Service in Docker

Stores recorded videos, looks them up by company name and serves them for playback in a browser. The Flask service runs behind Apache (mod_wsgi) in one container, PostgreSQL runs in another, and both are started together with Docker Compose.

This is the Test Task 2 video service reworked for Docker. The client that records and uploads videos is in [BrowserTesterTask](https://github.com/Vu1ture1/BrowserTesterTask).

## Architecture

```
client ──► http://127.0.0.1:8000 ──► web container (port 80)
                                       Apache
                                        ├─ /media/*  → files in /app/uploads (served by Apache)
                                        └─ /*        → mod_wsgi → Flask (web_service.py)
                                                                    │ psycopg2
                                                                    ▼
                                                      db container: PostgreSQL 16
```

| Service | Image | Purpose |
|---|---|---|
| `web` | built from `Dockerfile` | Apache + mod_wsgi + Flask, host port `8000` → container port `80` |
| `db` | `postgres:16` | Database; `web` starts only after `db` passes its healthcheck |

| Volume | Mounted at | Contents |
|---|---|---|
| `db_data` | `/var/lib/postgresql/data` | PostgreSQL data |
| `uploads` | `/app/uploads` | Uploaded video files |

Both volumes survive `docker compose down` / `up`.

## Requirements

- Docker Desktop (Windows / macOS) or Docker Engine with the Compose plugin (Linux)

## Setup

The database password is not stored in the repository. Copy the template and set your own password:

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

```
DB_NAME=videos
DB_USER=video_user
DB_PASSWORD=change_me
```

Docker Compose reads `.env` automatically and passes these values to both containers. If `DB_PASSWORD` is missing, Compose stops with an error instead of starting with an empty password.

## Usage

```bash
docker compose up --build
```

The first build takes a few minutes. The service is ready when the log shows:

```
web-1  | ... AH00489: Apache/2.4.x (Debian) mod_wsgi/x.x Python/3.12 configured -- resuming normal operations
```

Upload a video and get its URL (on Windows PowerShell use `curl.exe`):

```bash
curl -F "file=@Acme GmbH.webm" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload
curl "http://127.0.0.1:8000/get_video_url?company_name=ACME%20GmbH"
```

Or record and upload videos with the client:

```bash
python capture_video.py --server http://127.0.0.1:8000
```

Useful commands:

| Command | Action |
|---|---|
| `docker compose up -d --build` | Start in the background |
| `docker compose logs -f web` | Apache access log and application messages |
| `docker compose ps` | Container status (`db` should be `healthy`) |
| `docker compose exec web curl http://localhost/` | Check the service from inside the container |
| `docker compose down` | Stop and remove containers; data and videos are kept |
| `docker compose down -v` | Stop and also delete all data and videos |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/upload` | UploadVideo: multipart form with `file` and `company_name` |
| `GET` | `/get_video_url?company_name=<name>` | GetVideoURL |
| `GET`, `HEAD` | `/media/<filename>` | Serves an uploaded video (supports seeking) |
| `GET` | `/videos` | Lists all records |

`/upload` and `/get_video_url` return:

```json
{ "company_name": "ACME GmbH", "video_url": "http://127.0.0.1:8000/media/ACME_GmbH.webm" }
```

Errors return `{"error": "..."}` with status `400` (missing or empty field) or `404` (unknown company). Re-uploading the same `company_name` replaces the file and updates the existing record; no duplicate rows are created.

## How it works

- **Apache + mod_wsgi.** `apache/video_service.conf` defines the VirtualHost. Requests to `/media/` are served by Apache directly from `/app/uploads` (with Range support, half-written `*.part` files return 404). Everything else goes through `WSGIScriptAlias` to `web_service.wsgi`, which exposes the Flask app as `application`. The app runs in two mod_wsgi daemon processes as `www-data`.
- **mod_wsgi build.** The Dockerfile installs `mod-wsgi` with pip and registers it with `mod_wsgi-express module-config`, so the module always matches the image's Python.
- **Waiting for the database.** Compose starts `web` only when `db` is healthy (`pg_isready`). In addition, the app is preloaded when Apache starts and `init_db()` retries the connection up to 30 times, 2 seconds apart, so `web` does not crash if the database is restarted or slow.
- **Configuration.** Connection settings come only from the environment: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

## Storage

Videos are stored as files in the `uploads` volume; the database keeps only the path and URL. The table is created on first start:

```sql
CREATE TABLE IF NOT EXISTS videos (
    id           SERIAL      PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,  -- absolute path to the file on disk
    video_url    TEXT        NOT NULL,  -- public URL to the video
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

File names are derived from the company name: characters other than `A-Z a-z 0-9 . _ -` become `_`, so `ACME GmbH` is saved as `ACME_GmbH.webm`. If two different companies map to the same name, the second one gets a short hash suffix.

The video URL is built from the address of the incoming request, so it works both locally and from other machines on the network.

## Project files

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the Apache + mod_wsgi + Flask image |
| `docker-compose.yml` | Services `web` and `db`, volumes, healthcheck |
| `web_service.py` | Flask application (PostgreSQL via psycopg2) |
| `web_service.wsgi` | mod_wsgi entry point |
| `apache/video_service.conf` | Apache VirtualHost |
| `requirements.txt` | Python dependencies (`mod-wsgi` is installed only on Linux) |
| `.env.example` | Template for `.env` with database settings |

## Running without Docker (development)

With a local PostgreSQL, the app can also run on Flask's development server:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
$env:DB_NAME = "videos"; $env:DB_USER = "video_user"; $env:DB_PASSWORD = "..."
python web_service.py
```

The server listens on `0.0.0.0:8000`; set `FLASK_HOST` / `FLASK_PORT` to change it.
