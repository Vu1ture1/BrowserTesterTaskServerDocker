import hashlib
import json
import mimetypes
import os
import re
import time

import psycopg2
from psycopg2.extras import Json
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import apply_zoom

MAX_UPLOAD_BYTES = 1 << 30  # 1 GiB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(BASE_DIR, "uploads")
ZOOMED_SUBDIR = "zoomed"

def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError("Environment variable {} is not set.".format(name))
    return value

DB_PARAMS = {
    "host": os.environ.get("DB_HOST") or "localhost",
    "port": int(os.environ.get("DB_PORT") or 5432),
    "dbname": required_env("DB_NAME"),
    "user": required_env("DB_USER"),
    "password": required_env("DB_PASSWORD"),
    "connect_timeout": 5,
}

mimetypes.add_type("video/webm", ".webm")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.json.sort_keys = False

# --- Database ---

def get_db():
    """Open a new connection per request; psycopg2 connections must not be shared between threads."""
    return psycopg2.connect(**DB_PARAMS)

def init_db(attempts=30, delay=2.0):
    """Create the table, waiting for PostgreSQL if it is not accepting connections yet."""
    os.makedirs(UPLOADS_DIR, exist_ok=True)

    for attempt in range(1, attempts + 1):
        try:
            conn = get_db()
            break
        except psycopg2.OperationalError as e:
            if attempt == attempts:
                raise
            app.logger.warning("Database not ready (%s/%s): %s", attempt, attempts, str(e).strip())
            time.sleep(delay)

    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('videos_schema'))")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id           SERIAL      PRIMARY KEY,
                    company_name TEXT        NOT NULL UNIQUE,
                    file_path    TEXT        NOT NULL,  -- absolute path to the file on disk
                    video_url    TEXT        NOT NULL,  -- public URL to the video
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                ALTER TABLE videos
                    ADD COLUMN IF NOT EXISTS zoom_events         JSONB,
                    ADD COLUMN IF NOT EXISTS zoom_status         TEXT NOT NULL DEFAULT 'none',
                    ADD COLUMN IF NOT EXISTS zoom_error          TEXT,
                    ADD COLUMN IF NOT EXISTS zoom_started_at     TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS processed_file_path TEXT,
                    ADD COLUMN IF NOT EXISTS processed_video_url TEXT,
                    ADD COLUMN IF NOT EXISTS processed_at        TIMESTAMPTZ
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS videos_zoom_queue ON videos (updated_at)
                WHERE zoom_status IN ('pending', 'processing')
            """)
    finally:
        conn.close()

init_db()

# --- Endpoints ---

@app.get("/")
def index():
    return jsonify(
        service="video upload service",
        endpoints=[
            "POST /upload  (multipart: file, company_name, optional zoom = <video>.zoom.json)",
            "GET  /get_video_url?company_name=<name>",
            "GET  /media/<filename>",
            "GET  /videos",
            "POST /jobs/<id>/process  (re-run the zoom processing)",
        ],
    )

@app.post("/upload")
def upload():
    company_name = request.form.get("company_name", "").strip()
    file = request.files.get("file")

    if not company_name:
        return error(400, "Field 'company_name' is required.")
    if file is None:
        return error(400, "Field 'file' is required and must not be empty.")

    zoom_events = None
    zoom_file = request.files.get("zoom")
    if zoom_file is not None:
        try:
            zoom_events = json.loads(zoom_file.read().decode("utf-8-sig"))
            apply_zoom.parse_sidecar(zoom_events)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            return error(400, "Field 'zoom' is not a valid zoom sidecar: {}".format(e))
    zoom_status = "pending" if zoom_events is not None else "none"

    conn = get_db()
    try:
        file_name = make_file_name(conn, company_name, file.filename)
        file_path = os.path.join(UPLOADS_DIR, file_name)

        tmp_path = "{}.{}.part".format(file_path, os.urandom(16).hex())
        try:
            file.save(tmp_path)
            if os.path.getsize(tmp_path) == 0:
                return error(400, "Field 'file' is required and must not be empty.")
            replace_with_retry(tmp_path, file_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        video_url = "{}media/{}".format(request.host_url, file_name)

        zoom_json = Json(zoom_events) if zoom_events is not None else None
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT processed_file_path FROM videos WHERE company_name = %s FOR UPDATE",
                (company_name,),
            )
            old = cur.fetchone()
            cur.execute(
                """
                UPDATE videos
                SET file_path = %s, video_url = %s, updated_at = NOW(),
                    zoom_events = %s, zoom_status = %s, zoom_error = NULL, zoom_started_at = NULL,
                    processed_file_path = NULL, processed_video_url = NULL, processed_at = NULL
                WHERE company_name = %s
                """,
                (file_path, video_url, zoom_json, zoom_status, company_name),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO videos (company_name, file_path, video_url, zoom_events, zoom_status)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (company_name) DO UPDATE SET
                        file_path   = EXCLUDED.file_path,
                        video_url   = EXCLUDED.video_url,
                        zoom_events = EXCLUDED.zoom_events,
                        zoom_status = EXCLUDED.zoom_status,
                        updated_at  = NOW()
                    """,
                    (company_name, file_path, video_url, zoom_json, zoom_status),
                )
    finally:
        conn.close()

    if old and old[0] and os.path.exists(old[0]):
        try:
            os.remove(old[0])
        except OSError:
            pass

    app.logger.info("Uploaded %s -> %s (zoom: %s)", company_name, file_path, zoom_status)
    return jsonify(
        company_name=company_name,
        video_url=video_url,
        zoom_status=zoom_status,
        processed_video_url=None,
    )

@app.get("/get_video_url")
def get_video_url():
    company_name = request.args.get("company_name", "").strip()
    if not company_name:
        return error(400, "Query parameter 'company_name' is required.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT video_url, zoom_status, processed_video_url, zoom_error
                FROM videos WHERE company_name = %s
                """,
                (company_name,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return error(404, "Company '{}' not found.".format(company_name))
    result = {
        "company_name": company_name,
        "video_url": row[0],
        "zoom_status": row[1],
        "processed_video_url": row[2],
    }
    if row[3]:
        result["zoom_error"] = row[3]
    return jsonify(result)

@app.post("/jobs/<int:video_id>/process")
def process_job(video_id):
    """Queue the zoom processing again (e.g. after a failure); the worker picks it up."""
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT zoom_events IS NOT NULL, zoom_status FROM videos WHERE id = %s FOR UPDATE",
                (video_id,),
            )
            row = cur.fetchone()
            if row is None:
                return error(404, "Video {} not found.".format(video_id))
            if not row[0]:
                return error(409, "Video {} has no zoom events; upload it with a 'zoom' sidecar.".format(video_id))
            if row[1] == "processing":
                return error(409, "Video {} is being processed.".format(video_id))
            cur.execute(
                "UPDATE videos SET zoom_status = 'pending', zoom_error = NULL WHERE id = %s",
                (video_id,),
            )
    finally:
        conn.close()
    return jsonify(id=video_id, zoom_status="pending"), 202

@app.get("/media/<path:filename>")
def media(filename):
    if filename.endswith(".part"):
        return error(404, "File not found.")
    return send_from_directory(UPLOADS_DIR, filename, conditional=True)

@app.get("/videos")
def videos():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, company_name, file_path, video_url, created_at, updated_at,
                       zoom_status, processed_video_url, zoom_error
                FROM videos ORDER BY id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return jsonify([
        {
            "id": row[0],
            "company_name": row[1],
            "file_path": row[2],
            "video_url": row[3],
            "created_at": row[4].isoformat(),
            "updated_at": row[5].isoformat(),
            "zoom_status": row[6],
            "processed_video_url": row[7],
            "zoom_error": row[8],
        }
        for row in rows
    ])

@app.errorhandler(HTTPException)
def http_error(e):
    """Return every HTTP error (404, 405, 413, ...) as JSON, like the endpoints do."""
    message = "File not found." if e.code == 404 else e.description
    return error(e.code, message)

# --- Helpers ---

def error(status, message):
    return jsonify(error=message), status

def replace_with_retry(source, destination):
    """On Windows a file can't be replaced while another request is replacing it or a browser
    is streaming it; such locks are short, so retry for up to ~5 seconds."""
    for attempt in range(1, 51):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if attempt == 50:
                raise
            time.sleep(0.1)

def make_file_name(conn, company_name, uploaded_name):
    ext = os.path.splitext(uploaded_name or "")[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ".webm"

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", company_name).strip("_.")
    if not stem:
        stem = "video"

    candidate = stem + ext

    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM videos WHERE file_path = %s AND company_name <> %s LIMIT 1",
            (os.path.join(UPLOADS_DIR, candidate), company_name),
        )
        taken = cur.fetchone()
    if taken is None:
        return candidate

    digest = hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:8]
    return "{}_{}{}".format(stem, digest, ext)

if __name__ == "__main__":
    print("Uploads folder:", UPLOADS_DIR)
    print("Database:       {user}@{host}:{port}/{dbname}".format(**DB_PARAMS))
    app.run(
        host=os.environ.get("FLASK_HOST") or "0.0.0.0",
        port=int(os.environ.get("FLASK_PORT") or 8000),
        threaded=True,
    )
