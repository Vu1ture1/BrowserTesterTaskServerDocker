import json
import math
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apply_zoom as az

W, H = 1280, 720
FPS = 25
RAMP = 12

def ev(t, x, y, kind=None):
    e = {"t": t, "x": x, "y": y}
    if kind:
        e["type"] = kind
    return e

def segments_for(events, fps=FPS, ramp=RAMP):
    return az.build_segments(az.normalize_events(events), fps, ramp)

def eval_ffmpeg(expr, on, zoom=1.0, iw=W, ih=H):
    """Evaluate a zoompan expression in Python (only the functions apply_zoom uses)."""
    names = {
        "on": on, "zoom": zoom, "iw": iw, "ih": ih, "PI": math.pi,
        "cos": math.cos, "min": min,
        "clip": lambda v, lo, hi: min(max(v, lo), hi),
        "gte": lambda a, b: 1.0 if a >= b else 0.0,
        "if_": lambda c, a, b: a if c else b,
    }
    return eval(re.sub(r"\bif\(", "if_(", expr), {"__builtins__": {}}, names)

def filter_parts(vf):
    z = re.search(r"z='([^']*)'", vf).group(1)
    x = re.search(r"x='([^']*)'", vf).group(1)
    y = re.search(r"y='([^']*)'", vf).group(1)
    return z, x, y

# --- Ramp / easing ---

def test_ramp_frames():
    assert az.ramp_frames(0.5, 25) == 12
    assert az.ramp_frames(1.0, 30) == 30
    assert az.ramp_frames(0, 25) == 1

def test_ease_is_smooth_and_clamped():
    assert az.ease(-1) == 0
    assert az.ease(0) == 0
    assert az.ease(0.5) == pytest.approx(0.5)
    assert az.ease(1) == pytest.approx(1)
    assert az.ease(2) == pytest.approx(1)
    assert az.ease(0.1) < 0.1
    assert az.ease(0.9) > 0.9

def test_zoom_in_hold_out():
    segs = segments_for([ev(3.0, 640, 360, "in"), ev(8.0, 640, 360, "out")])
    assert segs == [{"start": 75, "end": 200, "x": 640, "y": 360}]

    level = lambda f: az.zoom_level(f, segs, 2.0, RAMP)
    assert level(0) == 1.0
    assert level(75) == 1.0
    assert level(75 + RAMP // 2) == pytest.approx(1.5)
    assert level(75 + RAMP) == pytest.approx(2.0)
    assert level(150) == 2.0
    assert level(200) == pytest.approx(2.0)
    assert level(200 + RAMP // 2) == pytest.approx(1.5)
    assert level(200 + RAMP) == pytest.approx(1.0)
    assert level(400) == 1.0

    ramp_in = [level(f) for f in range(75, 75 + RAMP + 1)]
    assert ramp_in == sorted(ramp_in)
    ramp_out = [level(f) for f in range(200, 200 + RAMP + 1)]
    assert ramp_out == sorted(ramp_out, reverse=True)

def test_out_during_ramp_in_does_not_jump():
    segs = segments_for([ev(1.0, 100, 100, "in"), ev(1.2, 100, 100, "out")])
    levels = [az.zoom_level(f, segs, 2.0, RAMP) for f in range(20, 60)]
    assert max(levels) < 2.0
    assert all(abs(a - b) < 0.2 for a, b in zip(levels, levels[1:]))

# --- Pairing ---

def test_multiple_pairs():
    segs = segments_for([
        ev(1, 100, 100, "in"), ev(3, 100, 100, "out"),
        ev(5, 900, 500, "in"), ev(7, 900, 500, "out"),
    ])
    assert [(s["start"], s["end"]) for s in segs] == [(25, 75), (125, 175)]
    assert az.zoom_level(50, segs, 2.0, RAMP) == 2.0
    assert az.zoom_level(100, segs, 2.0, RAMP) == 1.0
    assert az.zoom_level(150, segs, 2.0, RAMP) == 2.0
    assert az.active_point(50, segs) == (100, 100)
    assert az.active_point(150, segs) == (900, 500)

def test_open_ended_zoom_holds_to_end():
    segs = segments_for([ev(2, 640, 360, "in")])
    assert segs == [{"start": 50, "end": None, "x": 640, "y": 360}]
    assert az.zoom_level(50 + RAMP, segs, 2.0, RAMP) == pytest.approx(2.0)
    assert az.zoom_level(10 ** 6, segs, 2.0, RAMP) == pytest.approx(2.0)

def test_open_ended_after_closed_pair():
    segs = segments_for([ev(1, 0, 0, "in"), ev(2, 0, 0, "out"), ev(4, 50, 50, "in")])
    assert [(s["start"], s["end"]) for s in segs] == [(25, 50), (100, None)]

def test_unordered_events_are_sorted():
    segs = segments_for([ev(8, 1, 1, "out"), ev(3, 1, 1, "in")])
    assert [(s["start"], s["end"]) for s in segs] == [(75, 200)]

def test_stray_out_and_repeated_in_are_ignored():
    segs = segments_for([
        ev(0.5, 9, 9, "out"),
        ev(1, 100, 100, "in"), ev(2, 500, 500, "in"), ev(3, 100, 100, "out"),
        ev(4, 9, 9, "out"),
    ])
    assert segs == [{"start": 25, "end": 75, "x": 100, "y": 100}]

def test_overlapping_pair_is_moved_after_previous_zoom_out():
    segs = segments_for([ev(1, 0, 0, "in"), ev(2, 0, 0, "out"), ev(2.1, 0, 0, "in"), ev(3, 0, 0, "out")])
    assert segs[1]["start"] == 50 + RAMP
    for f in range(0, 120):
        assert 1.0 <= az.zoom_level(f, segs, 2.0, RAMP) <= 2.0

# --- Coordinates / clamping ---

def test_crop_centred_on_mouse():
    x, y, w, h = az.crop_box(200, segments_for([ev(0, 640, 360, "in")]), 2.0, RAMP, W, H)
    assert (w, h) == (640, 360)
    assert (x + w / 2, y + h / 2) == (640, 360)

    x, y, w, h = az.crop_box(200, segments_for([ev(0, 500, 300, "in")]), 2.0, RAMP, W, H)
    assert (x + w / 2, y + h / 2) == (500, 300)

@pytest.mark.parametrize("px,py,expected", [
    (0, 0, (0, 0)),
    (1280, 720, (640, 360)),
    (10, 700, (0, 360)),
    (1270, 5, (640, 0)),
])
def test_crop_clamped_to_frame(px, py, expected):
    x, y, w, h = az.crop_box(200, segments_for([ev(0, px, py, "in")]), 2.0, RAMP, W, H)
    assert (x, y) == expected
    assert 0 <= x and x + w <= W
    assert 0 <= y and y + h <= H

def test_crop_origin_any_zoom_stays_inside():
    for zoom in (1.0, 1.3, 2.0, 3.5):
        for center in (-50, 0, 100, 640, 1200, 1280, 1500):
            x = az.crop_origin(center, zoom, W)
            assert 0 <= x <= W - W / zoom + 1e-9

def test_zoom_one_is_full_frame():
    assert az.crop_box(0, segments_for([ev(5, 100, 100, "in")]), 2.0, RAMP, W, H) == (0, 0, W, H)

# --- ffmpeg filter ---

def settings_with(events, **kw):
    data = {"viewport": {"width": W, "height": H}, "fps": FPS, "zoom_factor": 2.0, "ramp_sec": 0.5, "events": events}
    data.update(kw)
    return az.parse_sidecar(data)

def check_filter_matches_python(settings, width, height, frames):
    """The zoompan expressions give the same zoom and crop as the Python reference."""
    ramp = az.ramp_frames(settings["ramp_sec"], settings["fps"])
    segs = az.build_segments(settings["events"], settings["fps"], ramp)
    vf = az.build_filter(settings, width, height)
    z_expr, x_expr, y_expr = filter_parts(vf)
    iw, ih = width * az.UPSCALE, height * az.UPSCALE
    sx, sy = width / settings["viewport"]["width"], height / settings["viewport"]["height"]
    for f in frames:
        zoom = eval_ffmpeg(z_expr, f)
        assert zoom == pytest.approx(az.zoom_level(f, segs, settings["zoom_factor"], ramp))
        x = eval_ffmpeg(x_expr, f, zoom, iw, ih)
        y = eval_ffmpeg(y_expr, f, zoom, iw, ih)
        cx, cy = az.active_point(f, segs)
        assert x == pytest.approx(az.crop_origin(cx * sx * az.UPSCALE, zoom, iw))
        assert y == pytest.approx(az.crop_origin(cy * sy * az.UPSCALE, zoom, ih))

def test_filter_expressions_match_reference():
    settings = settings_with([
        ev(1, 100, 650, "in"), ev(3, 100, 650, "out"),
        ev(4, 1200, 50, "in"), ev(6, 1200, 50, "out"),
        ev(7, 640, 360, "in"),
    ])
    check_filter_matches_python(settings, W, H, range(0, 260))

def test_filter_maps_viewport_to_video_pixels():
    settings = settings_with([ev(1, 320, 180, "in"), ev(2, 320, 180, "out")])
    check_filter_matches_python(settings, 640, 360, range(0, 80))

def test_filter_structure():
    vf = az.build_filter(settings_with([ev(3, 640, 360, "in"), ev(8, 640, 360, "out")]), W, H)
    assert vf.startswith("fps=25,scale={}:{}".format(W * az.UPSCALE, H * az.UPSCALE))
    assert ":d=1:fps=25:s=1280x720" in vf
    assert vf.endswith("format=yuv420p")

def test_command_has_vp9_and_keeps_resolution():
    cmd = az.build_command("ffmpeg", "in.webm", "out.webm", settings_with([ev(1, 1, 1)]), 1280, 720)
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-c:v") + 1] == "libvpx-vp9"
    assert "s=1280x720" in cmd[cmd.index("-vf") + 1]
    assert cmd[-1] == "out.webm"

# --- Sidecar ---

def test_parse_full_sidecar():
    s = az.parse_sidecar({
        "viewport": {"width": 1920, "height": 1080}, "fps": 30, "zoom_factor": 3, "ramp_sec": 1,
        "events": [ev(3.0, 640, 360, "in"), ev(8.0, 640, 360, "out")],
    })
    assert s["viewport"] == {"width": 1920, "height": 1080}
    assert (s["fps"], s["zoom_factor"], s["ramp_sec"]) == (30, 3.0, 1.0)
    assert [e["type"] for e in s["events"]] == ["in", "out"]

def test_parse_defaults():
    s = az.parse_sidecar({"events": [ev(1, 2, 3, "in")]})
    assert s["viewport"] == {"width": 1280, "height": 720}
    assert (s["fps"], s["zoom_factor"], s["ramp_sec"]) == (25, 2.0, 0.5)

def test_old_sidecar_without_type_toggles():
    s = az.parse_sidecar({"events": [ev(1, 0, 0), ev(2, 0, 0), ev(3, 0, 0)]})
    assert [e["type"] for e in s["events"]] == ["in", "out", "in"]
    segs = az.build_segments(s["events"], FPS, RAMP)
    assert [(x["start"], x["end"]) for x in segs] == [(25, 50), (75, None)]

def test_negative_time_clamped():
    s = az.parse_sidecar({"events": [ev(-0.2, 5, 5, "in")]})
    assert s["events"][0]["t"] == 0

def test_sidecar_path(tmp_path):
    video = str(tmp_path / "ACME_GmbH.webm")
    assert az.sidecar_path(video) == str(tmp_path / "ACME_GmbH.zoom.json")
    (tmp_path / "ACME_GmbH.webm.zoom.json").write_text("{}")
    assert az.sidecar_path(video) == video + ".zoom.json"

def test_load_missing_sidecar_means_no_events(tmp_path):
    assert az.load_sidecar(str(tmp_path / "nope.zoom.json"))["events"] == []

# --- apply_zoom() / CLI without ffmpeg ---

def make_video(tmp_path, events=None):
    video = tmp_path / "Acme.webm"
    video.write_bytes(b"fake webm data")
    if events is not None:
        (tmp_path / "Acme.zoom.json").write_text(json.dumps({"events": events}))
    return str(video)

@pytest.mark.parametrize("events", [None, []])
def test_no_events_copies_unchanged(tmp_path, events):
    video = make_video(tmp_path, events)
    result = az.apply_zoom(video, ffmpeg="definitely-not-ffmpeg")
    assert result["copied"] and result["command"] is None
    with open(result["output"], "rb") as f:
        assert f.read() == b"fake webm data"
    assert result["output"] == str(tmp_path / "Acme.zoomed.webm")

def test_dry_run_prints_command_and_does_not_encode(tmp_path, capsys):
    video = make_video(tmp_path, [ev(1, 640, 360, "in"), ev(2, 640, 360, "out")])
    assert az.main(["--video", video, "--dry-run", "--ffmpeg", "definitely-not-ffmpeg"]) == 0
    out = capsys.readouterr().out
    assert "definitely-not-ffmpeg" in out and "zoompan" in out and "libvpx-vp9" in out
    assert not os.path.exists(str(tmp_path / "Acme.zoomed.webm"))

def test_missing_ffmpeg_reports_error(tmp_path, capsys):
    video = make_video(tmp_path, [ev(1, 640, 360, "in")])
    assert az.main(["--video", video, "--ffmpeg", "definitely-not-ffmpeg"]) == 1
    assert "ffmpeg not found" in capsys.readouterr().err
    assert not os.path.exists(str(tmp_path / "Acme.zoomed.webm"))
