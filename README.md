# Video Upload Web Service in Docker

Stores recorded videos, looks them up by company name and serves them for playback in a browser. Videos uploaded with zoom events are post-processed in the background into a copy with a **Camtasia-style zoom** (smooth zoom-in → hold → zoom-out around the mouse position).

- **web**: Flask behind Apache (mod_wsgi)
- **worker**: renders the zoom with ffmpeg
- **db**: PostgreSQL 16

All three are started together with Docker Compose.

The client that records videos, marks zoom events (F5 or `window.__zoomAt`) and uploads them is in [BrowserTesterTask](https://github.com/Vu1ture1/BrowserTesterTask).

## Architecture

```
client ──► http://127.0.0.1:8000 ──► web container (port 80)
  POST /upload                          Apache
  file + zoom sidecar                    ├─ /media/*  → files in /app/uploads (served by Apache)
                                         └─ /*        → mod_wsgi → Flask (web_service.py)
                                                                     │ psycopg2
                                                                     ▼
                                                       db container: PostgreSQL 16
                                                                     ▲ zoom_status = 'pending'
                                                                     │
                                         worker container (worker.py) ┘
                                           apply_zoom.py + ffmpeg
                                           → /app/uploads/zoomed/<file>.webm
```

| Service | Image | Purpose |
|---|---|---|
| `web` | built from `Dockerfile` | Apache + mod_wsgi + Flask, host port `8000` → container port `80` |
| `worker` | same image, `python worker.py` | Picks up pending videos and renders the zoom with ffmpeg |
| `db` | `postgres:16` | Database; `web` and `worker` start only after `db` passes its healthcheck |

| Volume | Mounted at | Contents |
|---|---|---|
| `db_data` | `/var/lib/postgresql/data` (db) | PostgreSQL data |
| `uploads` | `/app/uploads` (web, worker) | Uploaded videos; zoomed copies in `zoomed/` |

Both volumes survive `docker compose down` / `up`.

## Requirements

- Docker Desktop (Windows / macOS) or Docker Engine with the Compose plugin (Linux)
- For running the unit tests or `apply_zoom.py` outside Docker: Python 3.9+, and ffmpeg for actual rendering

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

Docker Compose reads `.env` automatically and passes these values to all containers. If `DB_PASSWORD` is missing, Compose stops with an error instead of starting with an empty password.

## Usage

```bash
docker compose up --build
```

The first build takes a few minutes. The service is ready when the log shows:

```
web-1     | ... AH00489: Apache/2.4.x (Debian) mod_wsgi/x.x Python/3.12 configured -- resuming normal operations
worker-1  | ... worker INFO Worker started (uploads: /app/uploads, ffmpeg: ffmpeg)
```

### Record and upload with the client

From the [client repository](https://github.com/Vu1ture1/BrowserTesterTask):

```bash
python capture_video.py --server http://127.0.0.1:8000
```

```
C:\BrowserTesterTask\video\Acme GmbH.webm
Uploaded: http://127.0.0.1:8000/media/Acme_GmbH.webm
Zoomed: http://127.0.0.1:8000/media/zoomed/Acme_GmbH.webm
```

The client uploads `Acme GmbH.zoom.json` together with the video, waits for the worker and previews the zoomed copy.

### Upload manually

On Windows PowerShell use `curl.exe`:

```bash
# Plain upload
curl -F "file=@Acme GmbH.webm" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload

# Upload with zoom events: the worker renders a zoomed copy
curl -F "file=@Acme GmbH.webm" -F "zoom=@Acme GmbH.zoom.json" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload

# Poll until zoom_status is "done"
curl "http://127.0.0.1:8000/get_video_url?company_name=ACME%20GmbH"
```

### Useful commands

| Command | Action |
|---|---|
| `docker compose up -d --build` | Start in the background |
| `docker compose logs -f web` | Apache access log and application messages |
| `docker compose logs -f worker` | Zoom jobs: `Processing #id`, `Done #id ... -> url`, `Failed #id ...` |
| `docker compose ps` | Container status (`db` should be `healthy`) |
| `docker compose exec web curl http://localhost/` | Check the service from inside the container |
| `docker compose exec worker ffmpeg -version` | Check that ffmpeg is installed in the image |
| `docker compose down` | Stop and remove containers; data and videos are kept |
| `docker compose down -v` | Stop and also delete all data and videos |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/upload` | UploadVideo: multipart form with `file`, `company_name` and optional `zoom` (the `<video>.zoom.json` sidecar) |
| `GET` | `/get_video_url?company_name=<name>` | GetVideoURL, plus zoom status and the zoomed video URL |
| `POST` | `/jobs/<id>/process` | Queues the zoom processing of video `<id>` again (e.g. after a failure) |
| `GET`, `HEAD` | `/media/<filename>` | Serves an uploaded video (supports seeking) |
| `GET`, `HEAD` | `/media/zoomed/<filename>` | Serves a zoomed video |
| `GET` | `/videos` | Lists all records |

`/upload` and `/get_video_url` return:

```json
{
  "company_name": "ACME GmbH",
  "video_url": "http://127.0.0.1:8000/media/ACME_GmbH.webm",
  "zoom_status": "done",
  "processed_video_url": "http://127.0.0.1:8000/media/zoomed/ACME_GmbH.webm"
}
```

| `zoom_status` | Meaning |
|---|---|
| `none` | Uploaded without a `zoom` sidecar; nothing to process |
| `pending` | Waiting for the worker |
| `processing` | The worker is rendering it |
| `done` | `processed_video_url` is ready |
| `failed` | Rendering failed; the reason is in `zoom_error` (retry with `POST /jobs/<id>/process`) |

Errors return `{"error": "..."}` with one of these statuses:

- `400`: a field is missing or empty, or the `zoom` sidecar is invalid;
- `404`: unknown company or video id;
- `409`: `/jobs/<id>/process` on a video without zoom events, or one that is already being processed.

Re-uploading the same `company_name` replaces the file and updates the existing record; no duplicate rows are created. The old zoomed copy is deleted, and the video is processed again if the new upload has a `zoom` sidecar.

## Zoom post-processing

### Sidecar format

The client writes `<video>.zoom.json` next to the recording when at least one zoom event was marked:

```json
{
  "viewport": {"width": 1280, "height": 720},
  "fps": 25,
  "zoom_factor": 2.0,
  "ramp_sec": 0.5,
  "events": [
    {"t": 3.0, "x": 640, "y": 360, "type": "in"},
    {"t": 8.0, "x": 640, "y": 360, "type": "out"}
  ]
}
```

- `t`: seconds since the video start;
- `x`, `y`: mouse position in viewport pixels;
- `type`: `in` starts the zoom-in, `out` starts the zoom-out.

Missing settings fall back to the values above. Events without `type` (old sidecars) alternate `in` / `out` in time order, like F5 presses.

### apply_zoom.py

Renders the sidecar into a new `.webm` (VP9, same resolution as the input). It is used by the worker and also works as a standalone tool:

```bash
python apply_zoom.py --video video/ACME_GmbH.webm            # -> video/ACME_GmbH.zoomed.webm
python apply_zoom.py --video video/ACME_GmbH.webm --dry-run  # print the ffmpeg command, don't encode
```

| Option | Meaning |
|---|---|
| `--video PATH` | Input video (required) |
| `--sidecar PATH` | Zoom events; default `<video>.zoom.json` |
| `--output PATH` | Output video; default `<video>.zoomed.webm` |
| `--ffmpeg PATH` | ffmpeg binary if it is not on `PATH` (`ffprobe` is looked up next to it) |
| `--dry-run` | Print the ffmpeg command without encoding |

How the zoom is computed:

1. **Pairs.** Each `in` is paired with the next `out`: zoom-in (ramp) → hold until `out` → zoom-out (ramp).
   - Several pairs are supported.
   - An `in` without an `out` holds the zoom to the end of the video.
   - A repeated `in` while zoomed and an `out` without an `in` are ignored.
   - A pair that would overlap the previous zoom-out is moved after it.
2. **Easing.** The zoom level goes 1 → `zoom_factor` → 1 along a cosine ease-in/ease-out curve over `ramp_sec`, so it starts and stops smoothly instead of linearly.
3. **Coordinates.** Mouse pixels map to video pixels as `x * video_width / viewport_width`. The client records with `record_video_size == viewport`, so this is 1:1.
4. **Crop.** The visible region is `iw/zoom × ih/zoom`, centred on the mouse point and kept inside the frame: `x = clip(px − iw/(2·zoom), 0, iw − iw/zoom)`, and the same for `y`.
5. **ffmpeg.** The filter chain is `fps=25` → `scale` ×4 → `zoompan` → `format=yuv420p`.
   - `fps=25`: Playwright records with a variable frame rate.
   - `scale` ×4: `zoompan` crops at whole pixels; the upscale removes the jitter this causes during slow zooms.
   - `zoompan`: evaluates the same zoom and crop formulas per frame and outputs the input resolution.
6. **No events.** The video is copied unchanged and ffmpeg is not called.

The output is written to `<output>.part` and renamed when ffmpeg finishes, so a half-written file is never served (Apache returns 404 for `*.part`).

### Worker

`worker.py` runs in its own container from the same image (as `www-data`) and shares the `uploads` volume with `web`:

1. Every 2 seconds it takes the oldest `pending` video with `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`, so several workers never take the same job.
2. It writes `zoom_events` to a temporary sidecar and calls `apply_zoom.apply_zoom()`. The result goes to `/app/uploads/zoomed/<same file name>`.
3. It stores `processed_video_url`, `processed_file_path`, `processed_at` and `zoom_status = 'done'`, or `failed` with `zoom_error`.

It also handles these cases:
- **Re-upload during processing.** If the video was re-uploaded, the stale result is not saved; the new upload is already `pending`.
- **Stop.** On `docker compose stop` (SIGTERM) the current job goes back to `pending`.
- **Stuck jobs.** A job left in `processing` for more than 15 minutes (worker killed) is taken again.

| Environment variable | Default | Meaning |
|---|---|---|
| `WORKER_POLL_SECONDS` | `2` | Pause between queue checks when it is empty |
| `WORKER_STALE_MINUTES` | `15` | When a `processing` job counts as abandoned |
| `FFMPEG` | `ffmpeg` | ffmpeg binary |

## How the web service works

- **Apache + mod_wsgi.** `apache/video_service.conf` defines the VirtualHost.
  - Requests to `/media/` (including `/media/zoomed/`) are served by Apache directly from `/app/uploads`, with Range support. Half-written `*.part` files return 404.
  - Everything else goes through `WSGIScriptAlias` to `web_service.wsgi`, which exposes the Flask app as `application`. The app runs in two mod_wsgi daemon processes as `www-data`.
- **mod_wsgi build.** The Dockerfile installs `mod-wsgi` with pip and registers it with `mod_wsgi-express module-config`, so the module always matches the image's Python.
- **ffmpeg.** Installed from Debian packages in the same image; only the worker uses it.
- **Waiting for the database.** Compose starts `web` and `worker` only when `db` is healthy (`pg_isready`). In addition, `init_db()` retries the connection up to 30 times, 2 seconds apart, so the services do not crash if the database is restarted or slow.
- **Configuration.** Connection settings come only from the environment: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

## Storage

Videos are stored as files in the `uploads` volume; the database keeps the paths, URLs and zoom state. The table is created on first start. The zoom columns are added with `ADD COLUMN IF NOT EXISTS`, so a database from the previous version is upgraded in place.

```sql
CREATE TABLE IF NOT EXISTS videos (
    id                  SERIAL      PRIMARY KEY,
    company_name        TEXT        NOT NULL UNIQUE,
    file_path           TEXT        NOT NULL,  -- absolute path to the original video
    video_url           TEXT        NOT NULL,  -- public URL to the original video
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    zoom_events         JSONB,                 -- uploaded sidecar (NULL = no zoom)
    zoom_status         TEXT        NOT NULL DEFAULT 'none',
    zoom_error          TEXT,
    zoom_started_at     TIMESTAMPTZ,
    processed_file_path TEXT,                  -- /app/uploads/zoomed/<file>
    processed_video_url TEXT,                  -- public URL to the zoomed video
    processed_at        TIMESTAMPTZ
);
```

File names are derived from the company name: characters other than `A-Z a-z 0-9 . _ -` become `_`, so `ACME GmbH` is saved as `ACME_GmbH.webm` and its zoomed copy as `zoomed/ACME_GmbH.webm`. If two different companies map to the same name, the second one gets a short hash suffix.

The video URL is built from the address of the incoming request, so it works both locally and from other machines on the network.

## Tests

Unit tests for the zoom logic don't need ffmpeg or Docker. They cover:
- ramp frames and easing;
- in/out pairing, multiple pairs, and an open-ended zoom held to the end;
- centring on the mouse point and clamping to the frame;
- viewport → video mapping;
- sidecar parsing, including old sidecars without `type`;
- the "no events → copy unchanged" path, `--dry-run`, and a missing ffmpeg.

The ffmpeg `z`, `x` and `y` expressions are evaluated in Python and compared frame by frame with the reference math.

```bash
pip install pytest
python -m pytest -v tests/test_apply_zoom.py
```

Integration tests (`tests/test_docker_integration.py`) run against the running Docker stack. They check:
- ffmpeg with `libvpx-vp9` and `zoompan` is installed in the `web` and `worker` containers, and the worker runs as `www-data`;
- an upload with a sidecar goes `pending` → `done`, and the row in PostgreSQL holds the events and `processed_video_url`;
- Apache serves the zoomed video with Range support; it is VP9 1280×720 with the original duration, and the original is unchanged;
- the zoomed frames match the original before the first zoom and differ while zoomed, including the open-ended zoom at the end (SSIM);
- empty events give an identical copy, and old sidecars without `type` work;
- an upload without zoom stays `none`, and a re-upload removes the old zoomed copy;
- an invalid sidecar gives 400, a broken video gives `failed` with an error, and `POST /jobs/<id>/process` re-runs a job.

The test video is generated by ffmpeg inside the image, so no media files are needed. Test rows and files are deleted afterwards. If the service is not running, these tests are skipped.

```bash
docker compose up -d --build
python -m pytest -v tests              # unit + integration (~1 min)
```

Set `ZOOM_TEST_SERVER` to test a service at another address (default `http://127.0.0.1:8000`).

## Project files

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the Apache + mod_wsgi + Flask + ffmpeg image |
| `docker-compose.yml` | Services `web`, `worker` and `db`, volumes, healthcheck |
| `web_service.py` | Flask application (PostgreSQL via psycopg2) |
| `web_service.wsgi` | mod_wsgi entry point |
| `worker.py` | Background zoom worker |
| `apply_zoom.py` | Zoom post-processing with ffmpeg `zoompan` (also a CLI tool) |
| `tests/test_apply_zoom.py` | pytest unit tests for the zoom logic |
| `tests/test_docker_integration.py` | pytest integration tests for the Docker stack |
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
python web_service.py      # web service on 0.0.0.0:8000 (FLASK_HOST / FLASK_PORT to change)
python worker.py           # in a second terminal; needs ffmpeg on PATH or FFMPEG=<path>
```
