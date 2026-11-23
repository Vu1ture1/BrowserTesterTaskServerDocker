import json
import logging
import os
import signal
import subprocess
import tempfile
import time

import apply_zoom
from web_service import UPLOADS_DIR, ZOOMED_SUBDIR, get_db

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS") or 2)
STALE_MINUTES = int(os.environ.get("WORKER_STALE_MINUTES") or 15)
FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"

log = logging.getLogger("worker")

def claim_job(conn):
    """Take one pending video. SKIP LOCKED lets several workers run without taking the same job;
    a job stuck in 'processing' (worker killed) is taken again after STALE_MINUTES."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos
            SET zoom_status = 'processing', zoom_started_at = NOW(), zoom_error = NULL
            WHERE id = (
                SELECT id FROM videos
                WHERE zoom_status = 'pending'
                   OR (zoom_status = 'processing' AND zoom_started_at < NOW() - make_interval(mins => %s))
                ORDER BY updated_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, company_name, file_path, video_url, zoom_events, updated_at
            """,
            (STALE_MINUTES,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("id", "company_name", "file_path", "video_url", "zoom_events", "updated_at")
    return dict(zip(keys, row))

def finish_job(conn, job, status, processed_path=None, processed_url=None, error_message=None):
    """Save the result unless the video was re-uploaded while it was being processed
    (updated_at changed); in that case the new upload is already 'pending' again."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos
            SET zoom_status = %s, zoom_error = %s,
                processed_file_path = %s, processed_video_url = %s,
                processed_at = CASE WHEN %s = 'done' THEN NOW() END
            WHERE id = %s AND updated_at = %s AND zoom_status = 'processing'
            """,
            (status, error_message, processed_path, processed_url, status, job["id"], job["updated_at"]),
        )
        return cur.rowcount == 1

def processed_url_for(video_url, file_name):
    """http://host/media/ACME_GmbH.webm -> http://host/media/zoomed/ACME_GmbH.webm"""
    return "{}/{}/{}".format(video_url.rsplit("/", 1)[0], ZOOMED_SUBDIR, file_name)

def process(job):
    out_dir = os.path.join(UPLOADS_DIR, ZOOMED_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    file_name = os.path.basename(job["file_path"])
    output = os.path.join(out_dir, file_name)

    fd, sidecar = tempfile.mkstemp(suffix=".zoom.json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(job["zoom_events"], f)
        apply_zoom.apply_zoom(job["file_path"], output, sidecar, ffmpeg=FFMPEG)
    finally:
        os.remove(sidecar)
    return output, processed_url_for(job["video_url"], file_name)

def run_once(conn):
    """Process one job; returns False when the queue is empty."""
    job = claim_job(conn)
    if job is None:
        return False

    log.info("Processing #%s %s", job["id"], job["company_name"])
    started = time.monotonic()
    try:
        output, url = process(job)
    except (Exception, SystemExit, KeyboardInterrupt) as e:
        if isinstance(e, (SystemExit, KeyboardInterrupt)):
            with conn, conn.cursor() as cur:
                cur.execute("UPDATE videos SET zoom_status = 'pending' WHERE id = %s AND zoom_status = 'processing'", (job["id"],))
            raise
        if isinstance(e, subprocess.CalledProcessError):
            message = "ffmpeg failed with exit code {}".format(e.returncode)
        else:
            message = "{}: {}".format(type(e).__name__, e)
        log.error("Failed #%s %s: %s", job["id"], job["company_name"], message)
        finish_job(conn, job, "failed", error_message=message[:1000])
        return True

    if finish_job(conn, job, "done", output, url):
        log.info("Done #%s %s in %.1fs -> %s", job["id"], job["company_name"], time.monotonic() - started, url)
    else:
        log.info("Skipped #%s %s: re-uploaded during processing", job["id"], job["company_name"])
    return True

def stop(signum, frame):
    raise SystemExit(0)

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop)
    log.info("Worker started (uploads: %s, ffmpeg: %s)", UPLOADS_DIR, FFMPEG)

    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = get_db()
            if not run_once(conn):
                time.sleep(POLL_SECONDS)
        except (SystemExit, KeyboardInterrupt):
            log.info("Worker stopped")
            return
        except Exception as e:
            log.error("Worker error: %s", e)
            if conn is not None:
                conn.close()
            conn = None
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
