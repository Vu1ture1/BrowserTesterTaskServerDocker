import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = (os.environ.get("ZOOM_TEST_SERVER") or "http://127.0.0.1:8000").rstrip("/")
PROCESS_TIMEOUT = 180

SIDECAR = {
    "viewport": {"width": 1280, "height": 720},
    "fps": 25,
    "zoom_factor": 2.0,
    "ramp_sec": 0.5,
    "events": [
        {"t": 0.5, "x": 100, "y": 80, "type": "in"},
        {"t": 2.0, "x": 100, "y": 80, "type": "out"},
        {"t": 2.6, "x": 1200, "y": 650, "type": "in"},
    ],
}

# --- Helpers ---

def compose(*args, check=True):
    result = subprocess.run(
        ["docker", "compose", "--project-directory", PROJECT_DIR, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise AssertionError("docker compose {} failed:\n{}".format(" ".join(args), result.stderr))
    return result.stdout.strip()

def psql(sql):
    return compose("exec", "-T", "db", "sh", "-c", 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|" -c "$0"', sql)

def http(method, path, fields=None, files=None, headers=None):
    """Returns (status, headers, body bytes). files: {name: (filename, bytes)}."""
    url = path if path.startswith("http") else SERVER + path
    data = None
    headers = dict(headers or {})
    if fields is not None or files is not None:
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (fields or {}).items():
            parts.append('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}\r\n'.format(boundary, name, value).encode("utf-8"))
        for name, (filename, content) in (files or {}).items():
            parts.append('--{}\r\nContent-Disposition: form-data; name="{}"; filename="{}"\r\n'
                         'Content-Type: application/octet-stream\r\n\r\n'.format(boundary, name, filename).encode("utf-8"))
            parts.append(content + b"\r\n")
        parts.append("--{}--\r\n".format(boundary).encode("utf-8"))
        data = b"".join(parts)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()

def http_json(method, path, **kwargs):
    status, _, body = http(method, path, **kwargs)
    return status, json.loads(body.decode("utf-8"))

def upload(company, video, sidecar=None):
    files = {"file": ("video.webm", video)}
    if sidecar is not None:
        files["zoom"] = ("video.zoom.json", sidecar if isinstance(sidecar, bytes) else json.dumps(sidecar).encode("utf-8"))
    return http_json("POST", "/upload", fields={"company_name": company}, files=files)

def info(company):
    return http_json("GET", "/get_video_url?company_name=" + urllib.parse.quote(company))[1]

def wait_for(company, statuses=("done", "failed")):
    deadline = time.monotonic() + PROCESS_TIMEOUT
    while True:
        current = info(company)
        if current.get("zoom_status") in statuses or time.monotonic() > deadline:
            return current
        time.sleep(1)

def video_id(company):
    status, rows = http_json("GET", "/videos")
    return [r["id"] for r in rows if r["company_name"] == company][0]

def media_path(url):
    """http://host/media/zoomed/X.webm -> /app/uploads/zoomed/X.webm (inside the containers)."""
    return "/app/uploads/" + urllib.parse.urlparse(url).path.split("/media/", 1)[1]

# --- Fixtures ---

@pytest.fixture(scope="module")
def stack():
    try:
        status, _, _ = http("GET", "/")
    except (urllib.error.URLError, OSError) as e:
        pytest.skip("video service is not running at {} ({}); start it with docker compose up -d".format(SERVER, e))
    if status != 200:
        pytest.skip("video service at {} answered {}".format(SERVER, status))
    try:
        compose("ps")
    except (OSError, AssertionError) as e:
        pytest.skip("docker compose is not available: {}".format(e))

@pytest.fixture(scope="module")
def sample_video(stack):
    """A 4 s 1280x720 VP9 test video rendered by ffmpeg inside the image."""
    path = "/tmp/zoom_it_{}.webm".format(uuid.uuid4().hex)
    compose("exec", "-T", "worker", "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-t", "4",
            "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "40", "-deadline", "realtime", path)
    data = compose("exec", "-T", "worker", "python", "-c",
                   "import base64,sys; sys.stdout.write(base64.b64encode(open(sys.argv[1],'rb').read()).decode())", path)
    compose("exec", "-T", "worker", "rm", "-f", path)
    import base64
    return base64.b64decode(data)

@pytest.fixture
def company(stack):
    """A unique company name; its rows and files are removed after the test."""
    name = "Zoom IT {}".format(uuid.uuid4().hex[:8])
    yield name
    rows = psql("SELECT file_path, COALESCE(processed_file_path, '') FROM videos WHERE company_name = '{}'".format(name))
    for line in rows.splitlines():
        for path in line.split("|"):
            if path:
                compose("exec", "-T", "worker", "rm", "-f", path, check=False)
    psql("DELETE FROM videos WHERE company_name = '{}'".format(name))

# --- ffmpeg in the image ---

@pytest.mark.parametrize("service", ["web", "worker"])
def test_ffmpeg_installed_in_image(stack, service):
    out = compose("exec", "-T", service, "ffmpeg", "-hide_banner", "-version")
    assert out.startswith("ffmpeg version")
    encoders = compose("exec", "-T", service, "ffmpeg", "-hide_banner", "-encoders")
    assert "libvpx-vp9" in encoders
    filters = compose("exec", "-T", service, "ffmpeg", "-hide_banner", "-filters")
    assert " zoompan " in filters

def test_worker_service_is_running(stack):
    assert compose("exec", "-T", "worker", "id", "-un") == "www-data"
    assert "Worker started" in compose("logs", "worker")

# --- Processing step ---

def test_upload_with_zoom_is_processed_and_stored(company, sample_video):
    status, body = upload(company, sample_video, SIDECAR)
    assert status == 200
    assert body["zoom_status"] == "pending"
    assert body["processed_video_url"] is None

    result = wait_for(company)
    assert result["zoom_status"] == "done", result
    assert result["processed_video_url"].endswith("/media/zoomed/" + body["video_url"].rsplit("/", 1)[1])

    row = psql(
        "SELECT zoom_status, processed_video_url, jsonb_array_length(zoom_events->'events'), processed_at IS NOT NULL "
        "FROM videos WHERE company_name = '{}'".format(company)
    )
    assert row == "done|{}|3|t".format(result["processed_video_url"])

    status, headers, data = http("GET", result["processed_video_url"])
    assert status == 200 and headers["Content-Type"] == "video/webm" and len(data) > 1000
    status, _, data = http("GET", result["processed_video_url"], headers={"Range": "bytes=0-99"})
    assert status == 206 and len(data) == 100

    probe = compose("exec", "-T", "worker", "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name,width,height:format=duration", "-of", "csv=p=0",
                    media_path(result["processed_video_url"]))
    lines = probe.splitlines()
    assert lines[0] == "vp9,1280,720"
    assert abs(float(lines[1]) - 4.0) < 0.2
    assert http("GET", body["video_url"])[2] == sample_video

def test_zoomed_frames_differ_from_original(company, sample_video):
    upload(company, sample_video, SIDECAR)
    result = wait_for(company)
    assert result["zoom_status"] == "done", result

    def frame_md5(path, t):
        return compose("exec", "-T", "worker", "ffmpeg", "-v", "error", "-ss", str(t), "-i", path,
                       "-frames:v", "1", "-f", "md5", "-")

    original = media_path(result["video_url"])
    zoomed = media_path(result["processed_video_url"])
    assert frame_md5(original, 0.1) == frame_md5(zoomed, 0.1)   # before the first zoom: unchanged
    assert frame_md5(original, 1.5) != frame_md5(zoomed, 1.5)   # zoomed in
    assert frame_md5(original, 3.8) != frame_md5(zoomed, 3.8)   # open-ended zoom holds to the end

def test_sidecar_without_events_is_copied_unchanged(company, sample_video):
    upload(company, sample_video, {"events": []})
    result = wait_for(company)
    assert result["zoom_status"] == "done", result
    assert http("GET", result["processed_video_url"])[2] == sample_video

def test_legacy_sidecar_without_type(company, sample_video):
    legacy = {"events": [{"t": e["t"], "x": e["x"], "y": e["y"]} for e in SIDECAR["events"]]}
    upload(company, sample_video, legacy)
    assert wait_for(company)["zoom_status"] == "done"

def test_upload_without_zoom_is_not_processed(company, sample_video):
    status, body = upload(company, sample_video)
    assert status == 200 and body["zoom_status"] == "none"
    time.sleep(3)
    assert info(company)["zoom_status"] == "none"
    status, body = http_json("POST", "/jobs/{}/process".format(video_id(company)))
    assert status == 409

def test_reupload_without_zoom_removes_zoomed_copy(company, sample_video):
    upload(company, sample_video, SIDECAR)
    zoomed_url = wait_for(company)["processed_video_url"]
    assert http("GET", zoomed_url)[0] == 200

    upload(company, sample_video)
    result = info(company)
    assert result["zoom_status"] == "none" and result["processed_video_url"] is None
    assert http("GET", zoomed_url)[0] == 404

def test_invalid_sidecar_is_rejected(company, sample_video):
    status, body = upload(company, sample_video, b"{not json")
    assert status == 400 and "zoom" in body["error"]
    status, body = upload(company, sample_video, {"events": [{"t": 1}]})
    assert status == 400

def test_broken_video_fails_and_can_be_requeued(company):
    upload(company, b"this is not a video", SIDECAR)
    result = wait_for(company)
    assert result["zoom_status"] == "failed"
    assert "ffmpeg" in result["zoom_error"]

    status, body = http_json("POST", "/jobs/{}/process".format(video_id(company)))
    assert status == 202 and body["zoom_status"] == "pending"
    assert wait_for(company)["zoom_status"] == "failed"

def test_process_endpoint_reruns_job(company, sample_video):
    upload(company, sample_video, SIDECAR)
    assert wait_for(company)["zoom_status"] == "done"
    status, body = http_json("POST", "/jobs/{}/process".format(video_id(company)))
    assert status == 202
    assert wait_for(company)["zoom_status"] == "done"
    assert http_json("POST", "/jobs/999999999/process")[0] == 404
