"""What the Shelf player shows about a film besides the film: chapters and the scan-bar stills."""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.request

import pytest

from blueshelf import media, paths
from blueshelf.db import Database
from blueshelf.parser import parse_path
from blueshelf.server import RemoteServer

SCENE = "Site.24.03.05.Anna.Bell.Pool.Day.180_LR.mp4"


def _clip(path, seconds=8, chapters=()):
    """A small side-by-side clip, with chapters written into it if asked."""
    command = [paths.tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
               f"testsrc=size=640x320:rate=10:duration={seconds}"]
    if chapters:
        meta = path.with_suffix(".meta.txt")
        lines = [";FFMETADATA1"]
        for start, end, title in chapters:
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}",
                      f"END={int(end * 1000)}", f"title={title}"]
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        command += ["-i", str(meta), "-map_metadata", "1", "-map_chapters", "1"]
    # A key frame a second, as real films have; the stills are taken from key frames.
    command += ["-c:v", "libopenh264", "-g", "10", "-pix_fmt", "yuv420p", str(path)]
    paths.run_tool(command, timeout=120)
    return path


@pytest.fixture()
def running(tmp_path, monkeypatch):
    data, library = tmp_path / "data", tmp_path / "library"
    (data / "thumbs").mkdir(parents=True)
    library.mkdir()
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "thumbs_dir", lambda: data / "thumbs")
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    monkeypatch.setattr(media, "STRIPS", media.StripMaker())
    video = _clip(library / SCENE, chapters=[(0, 3, "Hello"), (3, 6, "The Pool"), (6, 8, "Goodbye")])
    db = Database(str(data / "library.db"))
    library_id = db.add_library(str(library))
    video_id = db.insert_video(library_id, str(video), SCENE, video.stat().st_size, 1.0,
                               parse_path(SCENE), True)
    db.set_fields(video_id, duration=8.0, width=640, height=320, is_vr=1, vr_layout=1, vr_proj=0)
    db.commit()
    server = RemoteServer(token="secret", port=0, secure=False)
    server.start()
    yield server, db, video_id, video
    server.stop()
    db.close()


def fetch(server, route, payload=None):
    url = f"http://127.0.0.1:{server.port}{route}{'&' if '?' in route else '?'}k=secret"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.headers, response.read()


def test_a_film_opened_lists_its_chapters_and_a_listing_does_not_look(running):
    server, _db, video_id, _video = running
    _headers, body = fetch(server, f"/api/video/{video_id}")
    told = json.loads(body)["chapters"]
    assert [c["title"] for c in told] == ["Hello", "The Pool", "Goodbye"]
    assert [round(c["start"]) for c in told] == [0, 3, 6]

    _headers, body = fetch(server, "/api/library")
    assert json.loads(body)[0]["chapters"] == [], "every film in a listing would mean opening every file"


def test_chapters_are_asked_for_once_until_the_file_changes(tmp_path, monkeypatch):
    video = _clip(tmp_path / "a.mp4", seconds=2, chapters=[(0, 2, "Only")])
    asked = []
    real = paths.run_tool
    monkeypatch.setattr(paths, "run_tool", lambda args, **k: asked.append(args) or real(args, **k))
    assert media.chapters(str(video))[0]["title"] == "Only"
    assert media.chapters(str(video))[0]["title"] == "Only"
    assert len(asked) == 1
    _clip(video, seconds=3, chapters=[(0, 3, "Changed")])
    assert media.chapters(str(video))[0]["title"] == "Changed"


def test_a_film_without_chapters_has_none(tmp_path):
    assert media.chapters(str(_clip(tmp_path / "plain.mp4", seconds=1))) == []
    assert media.chapters(str(tmp_path / "missing.mp4")) == []


def test_the_stills_are_made_once_asked_for_and_then_served(running):
    server, _db, video_id, _video = running
    _headers, body = fetch(server, f"/api/strip/{video_id}")
    first = json.loads(body)
    assert first["making"] and not first["ready"] and first["count"] == 100, "asking starts it"
    assert 0 <= first["done"] <= 100, "and says how far along it is"
    deadline = time.time() + 120
    while time.time() < deadline:
        told = json.loads(fetch(server, f"/api/strip/{video_id}")[1])
        if told.get("ready") or told.get("failed"):
            break
        time.sleep(0.5)
    assert told.get("ready"), told
    assert told["count"] == 100 and told["cols"] == 10

    headers, picture = fetch(server, told["url"])
    assert headers["Content-Type"] == "image/jpeg"
    sheet = media.strip_path(video_id)
    size = paths.run_tool([paths.tool("ffprobe"), "-v", "error", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(sheet)], timeout=30).stdout.decode().strip()
    assert size == f"{10 * media.TILE_W},{10 * media.TILE_H}"


def test_a_film_that_cannot_be_read_is_not_tried_again_and_again(running):
    server, db, video_id, video = running
    video.write_bytes(b"not a film" * 100)
    fetch(server, f"/api/strip/{video_id}")
    deadline = time.time() + 60
    while time.time() < deadline:
        told = json.loads(fetch(server, f"/api/strip/{video_id}")[1])
        if not told.get("making"):
            break
        time.sleep(0.3)
    assert told == {"ready": False, "failed": True}


def test_correcting_the_lens_to_flat_makes_it_an_ordinary_film_and_its_stills_again(running):
    server, _db, video_id, _video = running
    sheet = media.strip_path(video_id)
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_bytes(b"old stills")
    fetch(server, f"/api/video/{video_id}/lens", {"flat": True, "proj": 0, "fov": 180, "layout": 1})
    told = json.loads(fetch(server, f"/api/video/{video_id}")[1])
    assert told["is_vr"] is False and told["vr_layout"] == 1, "a 3D film for a flat screen stays side by side"
    assert not sheet.exists(), "stills cut for the old shape are thrown away"


def test_the_fonts_go_out_as_fonts():
    import blueshelf.server  # noqa: F401 - registering the type is part of importing it
    assert mimetypes.guess_type("fonts/figtree-latin.woff2")[0] == "font/woff2"
