import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys

DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
DEFAULT_FPS = 25
DEFAULT_ZOOM_FACTOR = 2.0
DEFAULT_RAMP_SEC = 0.5

UPSCALE = 4

def sidecar_path(video_path):
    """<video>.zoom.json: ACME_GmbH.webm -> ACME_GmbH.zoom.json (ACME_GmbH.webm.zoom.json is also accepted)."""
    stem = os.path.splitext(video_path)[0] + ".zoom.json"
    full = video_path + ".zoom.json"
    if not os.path.exists(stem) and os.path.exists(full):
        return full
    return stem

def load_sidecar(path):
    """Read a sidecar; a missing file means no zoom. Fills in defaults for absent settings."""
    if not path or not os.path.exists(path):
        data = {}
    else:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    return parse_sidecar(data)

def parse_sidecar(data):
    viewport = data.get("viewport") or DEFAULT_VIEWPORT
    return {
        "viewport": {"width": int(viewport["width"]), "height": int(viewport["height"])},
        "fps": float(data.get("fps") or DEFAULT_FPS),
        "zoom_factor": float(data.get("zoom_factor") or DEFAULT_ZOOM_FACTOR),
        "ramp_sec": float(data.get("ramp_sec") if data.get("ramp_sec") is not None else DEFAULT_RAMP_SEC),
        "events": normalize_events(data.get("events") or []),
    }

def normalize_events(events):
    """Sort by time; events without "type" (old sidecars) toggle in/out in order, like F5 presses."""
    events = sorted(events, key=lambda e: float(e["t"]))
    result = []
    zoomed = False
    for e in events:
        kind = e.get("type")
        if kind not in ("in", "out"):
            kind = "out" if zoomed else "in"
        zoomed = kind == "in"
        result.append({"t": max(0.0, float(e["t"])), "x": float(e["x"]), "y": float(e["y"]), "type": kind})
    return result

def ramp_frames(ramp_sec, fps):
    return max(1, int(round(ramp_sec * fps)))

def to_frame(t, fps):
    return int(round(t * fps))

def build_segments(events, fps, ramp):
    """Pair each "in" with the next "out".

    Returns [{"start", "end", "x", "y"}] in frames: zoom-in starts at "start", zoom-out starts at "end"
    (None = hold to the end of the video). The zoom stays centred on the "in" point.
    A repeated "in" while zoomed and an "out" without an "in" are ignored. A pair that would start
    before the previous zoom-out has finished is moved to its end, so pairs never overlap.
    """
    segments = []
    current = None
    for e in events:
        if e["type"] == "in" and current is None:
            current = {"start": to_frame(e["t"], fps), "end": None, "x": e["x"], "y": e["y"]}
            if segments:
                current["start"] = max(current["start"], segments[-1]["end"] + ramp)
        elif e["type"] == "out" and current is not None:
            current["end"] = max(to_frame(e["t"], fps), current["start"])
            segments.append(current)
            current = None
    if current is not None:
        segments.append(current)
    return segments

def ease(p):
    """Cosine ease-in-out on 0..1 (same curve as the cursor movement in qscript.py)."""
    p = min(1.0, max(0.0, p))
    return (1 - math.cos(math.pi * p)) / 2

def zoom_level(frame, segments, factor, ramp):
    """Zoom for an output frame: 1 outside the segments, factor while holding, eased in between."""
    for s in segments:
        up = ease((frame - s["start"]) / ramp)
        down = 1.0 if s["end"] is None else 1 - ease((frame - s["end"]) / ramp)
        level = min(up, down)
        if level > 0:
            return 1 + (factor - 1) * level
    return 1.0

def active_point(frame, segments):
    """Zoom centre for a frame: the point of the latest segment that has started."""
    point = (segments[0]["x"], segments[0]["y"]) if segments else (0.0, 0.0)
    for s in segments:
        if frame >= s["start"]:
            point = (s["x"], s["y"])
    return point

def crop_origin(center, zoom, size):
    """Left/top edge of the crop (size/zoom wide) centred on center, kept inside 0..size."""
    return min(max(center - size / (2 * zoom), 0.0), size - size / zoom)

def crop_box(frame, segments, factor, ramp, width, height):
    """(x, y, w, h) of the visible region of a width x height frame."""
    zoom = zoom_level(frame, segments, factor, ramp)
    cx, cy = active_point(frame, segments)
    return (crop_origin(cx, zoom, width), crop_origin(cy, zoom, height), width / zoom, height / zoom)

def num(value):
    return "{:.6g}".format(value)

def zoom_expr(segments, factor, ramp):
    """zoompan z= expression: the same curve as zoom_level(), evaluated per output frame "on"."""
    if not segments:
        return "1"
    terms = []
    for s in segments:
        up = "(1-cos(PI*clip((on-{})/{},0,1)))/2".format(s["start"], ramp)
        if s["end"] is None:
            terms.append(up)
        else:
            down = "(1+cos(PI*clip((on-{})/{},0,1)))/2".format(s["end"], ramp)
            terms.append("min({},{})".format(up, down))
    return "1+{}*({})".format(num(factor - 1), "+".join(terms))

def center_expr(segments, coord, viewport_size, input_size):
    """Zoom centre in input pixels: viewport px * iw/viewport width (1:1 for record_video_size == viewport)."""
    key = "x" if coord == "x" else "y"
    expr = num(segments[0][key]) if segments else "0"
    for s in segments[1:]:
        expr = "if(gte(on,{}),{},{})".format(s["start"], num(s[key]), expr)
    return "({})*{}/{}".format(expr, input_size, viewport_size)

def position_expr(segments, coord, viewport_size):
    """zoompan x=/y=: clip(px - iw/(2*zoom), 0, iw - iw/zoom)."""
    size = "iw" if coord == "x" else "ih"
    center = center_expr(segments, coord, viewport_size, size)
    return "clip({c}-{s}/(2*zoom),0,{s}-{s}/zoom)".format(c=center, s=size)

def build_filter(settings, width, height):
    fps = settings["fps"]
    ramp = ramp_frames(settings["ramp_sec"], fps)
    segments = build_segments(settings["events"], fps, ramp)
    vw = settings["viewport"]["width"]
    vh = settings["viewport"]["height"]
    return (
        "fps={fps},scale={uw}:{uh}:flags=lanczos,"
        "zoompan=z='{z}':x='{x}':y='{y}':d=1:fps={fps}:s={w}x{h},"
        "format=yuv420p"
    ).format(
        fps=num(fps),
        uw=width * UPSCALE,
        uh=height * UPSCALE,
        z=zoom_expr(segments, settings["zoom_factor"], ramp),
        x=position_expr(segments, "x", vw),
        y=position_expr(segments, "y", vh),
        w=width,
        h=height,
    )

def build_command(ffmpeg, video, output, settings, width, height):
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video,
        "-vf", build_filter(settings, width, height),
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
        "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
        "-c:a", "copy",
        "-f", "webm", output,
    ]

def probe_size(ffmpeg, video):
    """Video width/height via ffprobe next to ffmpeg; None if it is not available."""
    folder, name = os.path.split(ffmpeg)
    ffprobe = os.path.join(folder, name.replace("ffmpeg", "ffprobe")) if "ffmpeg" in name else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", video],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        width, height = out.splitlines()[0].split("x")
        return int(width), int(height)
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return None

def format_command(cmd):
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(c) for c in cmd)

def default_output(video_path):
    return os.path.splitext(video_path)[0] + ".zoomed.webm"

def apply_zoom(video, output=None, sidecar=None, ffmpeg="ffmpeg", dry_run=False):
    """Render the zoom. Returns {"output", "command", "copied"}; command is None when nothing to zoom."""
    output = output or default_output(video)
    settings = load_sidecar(sidecar or sidecar_path(video))
    result = {"output": output, "command": None, "copied": False}

    if not build_segments(settings["events"], settings["fps"], ramp_frames(settings["ramp_sec"], settings["fps"])):
        result["copied"] = True
        if not dry_run:
            copy_atomic(video, output)
        return result

    size = probe_size(ffmpeg, video) or (settings["viewport"]["width"], settings["viewport"]["height"])
    tmp = output + ".part"
    cmd = build_command(ffmpeg, video, tmp, settings, size[0], size[1])
    result["command"] = cmd[:-1] + [output]
    if dry_run:
        return result

    try:
        subprocess.run(cmd, check=True)
        os.replace(tmp, output)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return result

def copy_atomic(src, dst):
    if os.path.abspath(src) == os.path.abspath(dst):
        return
    tmp = dst + ".part"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)

def main(argv=None):
    parser = argparse.ArgumentParser(description="Render Camtasia-style zoom events into a video.")
    parser.add_argument("--video", required=True, help="input .webm")
    parser.add_argument("--output", help="output .webm (default: <video>.zoomed.webm)")
    parser.add_argument("--sidecar", help="zoom events (default: <video>.zoom.json)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="path to the ffmpeg binary if it is not on PATH")
    parser.add_argument("--dry-run", action="store_true", help="print the ffmpeg command without encoding")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.video):
        parser.error("video not found: {}".format(args.video))

    try:
        result = apply_zoom(args.video, args.output, args.sidecar, args.ffmpeg, args.dry_run)
    except FileNotFoundError:
        print("ffmpeg not found: {} (install it or pass --ffmpeg <path>)".format(args.ffmpeg), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print("ffmpeg failed with exit code {}".format(e.returncode), file=sys.stderr)
        return 1

    if result["copied"]:
        print("No zoom events, {} {}".format("would copy to" if args.dry_run else "copied unchanged to", result["output"]))
    elif args.dry_run:
        print(format_command(result["command"]))
    else:
        print("Saved {}".format(result["output"]))
    return 0

if __name__ == "__main__":
    sys.exit(main())