"""Scenes marked while watching, and made into a passthrough copy of their own."""
from __future__ import annotations

import json
import subprocess
import urllib.request

import pytest

from blueshelf import passthrough, paths
from blueshelf.db import Database
from blueshelf.parser import parse_path
from blueshelf.passthrough import Job
from blueshelf.server import RemoteServer

SCENE = "Site.24.03.05.Anna.Bell.Pool.Day.180_LR.mp4"


def _film(path, seconds=20):
    """A film whose red rises with time - 12 levels a second - so any frame says when it is
    from. A key frame every second, as real films have, and sound."""
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:s=64x64:r=10:d={seconds},geq=r='T*12':g=0:b=0",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libopenh264", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(path)], timeout=120)
    return path


def _red_at(path, seconds):
    said = subprocess.run([paths.tool("ffmpeg"), "-v", "error", "-ss", f"{seconds}", "-i", str(path),
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, timeout=60)
    return said.stdout[(32 * 64 + 32) * 3]


def _streams(path):
    said = paths.run_tool([paths.tool("ffprobe"), "-v", "error", "-show_entries",
                           "stream=codec_type:format=duration", "-of", "json", str(path)], timeout=60)
    data = json.loads(said.stdout)
    return [s["codec_type"] for s in data["streams"]], float(data["format"]["duration"])


# --------------------------------------------------------------------------- keeping them

@pytest.fixture()
def db(tmp_path):
    base = Database(str(tmp_path / "library.db"))
    library = base.add_library(str(tmp_path))
    (tmp_path / SCENE).write_bytes(b"x")
    video = base.insert_video(library, str(tmp_path / SCENE), SCENE, 1, 1.0, parse_path(SCENE), True)
    yield base, video
    base.close()


def test_scenes_are_kept_in_film_order_and_numbered_as_seen(db):
    base, video = db
    base.add_scene(video, 300, 420)
    base.add_scene(video, 60, 90, "The pool")
    base.add_scene(video, 500, 480)          # marked end first: put the right way round
    told = base.scenes(video)
    assert [(s["start"], s["end"]) for s in told] == [(60, 90), (300, 420), (480, 500)]
    assert [s["label"] for s in told] == ["The pool", "Scene 2", "Scene 3"]
    assert [s["named"] for s in told] == [True, False, False]


def test_a_scene_too_short_to_be_meant_is_refused(db):
    base, video = db
    with pytest.raises(ValueError):
        base.add_scene(video, 10, 11)
    scene = base.add_scene(video, 10, 20)
    with pytest.raises(ValueError):
        base.update_scene(scene, end=10.5)
    assert base.scenes(video)[0]["end"] == 20, "a refused change leaves the scene as it was"


def test_scenes_can_be_moved_renamed_and_forgotten_and_go_with_their_film(db):
    base, video = db
    scene = base.add_scene(video, 10, 20)
    base.update_scene(scene, start=12, label="Better")
    assert base.scenes(video)[0]["start"] == 12 and base.scenes(video)[0]["label"] == "Better"
    assert base.videos_with_scenes()[0]["n"] == 1
    base.delete_scene(scene)
    assert base.scenes(video) == [] and base.videos_with_scenes() == []
    base.add_scene(video, 10, 20)
    base.delete_videos([video])
    assert base.conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0] == 0


# --------------------------------------------------------------------------- from the headset

@pytest.fixture()
def running(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    film = tmp_path / SCENE
    film.write_bytes(b"x")
    base = Database(str(data / "library.db"))
    library = base.add_library(str(tmp_path))
    video = base.insert_video(library, str(film), SCENE, 1, 1.0, parse_path(SCENE), True)
    other = base.insert_video(library, str(tmp_path / "other.mp4"), "other.mp4", 1, 1.0, parse_path("other.mp4"), True)
    base.commit()
    server = RemoteServer(token="secret", port=0, secure=False)
    server.start()
    yield server, base, video, other
    server.stop()
    base.close()


def post(server, video, action, payload):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/secret/api/video/{video}/{action}",
                                     data=json.dumps(payload).encode(), method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_the_headset_marks_moves_and_forgets_scenes(running):
    server, base, video, other = running
    told = post(server, video, "scene-add", {"start": 95.5, "end": 30})
    assert told["ok"] and [(s["start"], s["end"]) for s in told["scenes"]] == [(30, 95.5)]
    scene = told["scenes"][0]["id"]
    told = post(server, video, "scene-change", {"id": scene, "end": 120, "label": "Pool"})
    assert told["scenes"][0]["end"] == 120 and told["scenes"][0]["label"] == "Pool"
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/secret/api/video/{video}", timeout=10) as r:
        assert [s["id"] for s in json.loads(r.read())["scenes"]] == [scene], "a film opened lists its scenes"
    told = post(server, video, "scene-add", {"start": 5, "end": 5.5})
    assert not told["ok"] and "2 seconds" in told["error"] and len(told["scenes"]) == 1
    told = post(server, other, "scene-delete", {"id": scene})
    assert not told["ok"], "another film's scene cannot be touched through this one"
    told = post(server, video, "scene-delete", {"id": scene})
    assert told["ok"] and told["scenes"] == []


# --------------------------------------------------------------------------- into a clip

def test_chosen_scenes_are_cut_out_in_the_chosen_order_with_their_sound(tmp_path):
    film = _film(tmp_path / SCENE)
    clip = passthrough.scene_clip_path(film, "the best bits")
    said = []
    made = passthrough.cut_scenes(film, [(12.0, 15.0), (3.0, 6.0)], clip, lambda s, f, d: said.append(s))
    assert made == clip and clip.is_file()
    kinds, length = _streams(clip)
    assert kinds == ["video", "audio"], "the sound comes too"
    assert 5.8 <= length <= 8.2, length          # 3 + 3 seconds, each start up to a key frame early
    first, second = _red_at(clip, 0.05), _red_at(clip, 3.2)
    assert 125 <= first <= 150, f"it opens on the scene from 12 s: red {first}"
    assert 25 <= second <= 60, f"then the scene from 3 s: red {second}"
    assert passthrough.frame_timing(clip)[0] in ("cfr", "unknown"), "evenly spaced, or the mask would drift"
    assert not list(clip.parent.glob("*.part*")), "the pieces are cleared away"
    assert any("Cutting scene 2 of 2" in line for line in said)


def test_one_scene_is_cut_without_a_second_copy(tmp_path):
    film = _film(tmp_path / SCENE, seconds=8)
    clip = passthrough.cut_scenes(film, [(2.0, 5.0)], passthrough.scene_clip_path(film, "one"))
    assert 2.8 <= _streams(clip)[1] <= 4.2


def test_a_clip_of_scenes_is_kept_out_of_sight_and_its_copy_goes_beside_the_film(tmp_path):
    film = tmp_path / "films" / SCENE
    clip = passthrough.scene_clip_path(film, 'Pool: "day" / night?')
    assert clip.parent == tmp_path / "films" / f".{film.stem}.scenes", "a dot folder the library skips"
    assert clip.name == f"{film.stem} - Pool day  night.mp4"
    job = passthrough.inspect(Job(source=str(clip)), {"width": 7200, "height": 3600, "fps": 60.0, "frames": 100})
    assert passthrough.Path(job.output).parent == film.parent.resolve()
    assert passthrough.Path(job.output).name.startswith(film.stem.replace("_180_LR", "")[:20])
    assert job.output.endswith("_alpha.mp4")
    assert passthrough.work_folder(job).parent == film.parent.resolve(), "its work beside the film too"
