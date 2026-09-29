"""The DeoVR feed: what DeoVR is told about each scene, and how it gets in."""
from __future__ import annotations

import http.client
import json
import ssl
from pathlib import Path

import pytest

from blueshelf import deovr, paths
from blueshelf.db import Database
from blueshelf.parser import parse_path
from blueshelf.server import RemoteServer

FLAT = "Site.24.03.05.Anna.Bell.Pool.Day.XXX.mp4"
VR = "VRStudio.Sunset.mp4"
SECRET = "Site.24.03.06.Nobody.Hidden.Scene.XXX.mp4"


@pytest.fixture()
def running(tmp_path, monkeypatch):
    data, library = tmp_path / "data", tmp_path / "library"
    (data / "thumbs").mkdir(parents=True)
    library.mkdir()
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(data))
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "thumbs_dir", lambda: data / "thumbs")
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")

    db = Database(str(data / "library.db"))
    lib_id = db.add_library(str(library))
    ids = {}
    for name in (FLAT, VR, SECRET):
        path = library / name
        path.write_bytes(bytes(range(256)) * 40)
        ids[name] = db.insert_video(lib_id, str(path), name, path.stat().st_size, 1.0, parse_path(name), True)
    db.set_fields(ids[FLAT], duration=3725.5, width=1920, height=1080, thumb=f"{ids[FLAT]}.jpg", vcodec="h264")
    db.set_fields(ids[VR], duration=600.0, width=5760, height=2880, is_vr=1, vr_layout=1, vr_proj=0,
                  title="Sunset", vcodec="hevc")
    db.set_performers(ids[VR], ["Anna Bell"])
    db.set_fields(ids[SECRET], hidden=1)
    db.commit()

    server = RemoteServer(token="word", port=0)
    server.start()
    yield server, db, ids
    server.stop()
    db.close()


def fetch(server, route, secure=True, method="GET", body=None):
    if secure:
        loose = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        loose.check_hostname = False
        loose.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", server.port, context=loose, timeout=10)
    else:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, route, payload, {"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), data


# --------------------------------------------------------------------------- what a scene says

@pytest.mark.parametrize("video, screen, stereo, three_d", [
    ({"is_vr": False}, "flat", "off", False),
    ({"is_vr": True, "vr_proj": 0, "vr_layout": 1}, "dome", "sbs", True),
    ({"is_vr": True, "vr_proj": 0, "vr_layout": 2}, "dome", "tb", True),
    ({"is_vr": True, "vr_proj": 1, "vr_layout": 0}, "sphere", "off", False),
    ({"is_vr": True, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 180}, "fisheye", "sbs", True),
    ({"is_vr": True, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 190}, "rf52", "sbs", True),
    ({"is_vr": True, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 200}, "mkx200", "sbs", True),
    ({"is_vr": True, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 220}, "vrca220", "sbs", True),
])
def test_every_projection_is_named_the_way_deovr_names_it(video, screen, stereo, three_d):
    video = {"id": 1, "path": "x.mp4", "title": "x", **video}
    assert deovr.screen_type(video) == screen
    assert deovr.stereo_mode(video) == stereo
    assert deovr.full_entry(video, "http://pc:8760/word")["is3d"] is three_d


def test_a_scene_carries_where_to_play_it(running):
    server, _db, ids = running
    status, _headers, body = fetch(server, f"/word/deovr/video/{ids[VR]}")
    assert status == 200
    scene = json.loads(body)
    assert scene["title"] == "Sunset" and scene["videoLength"] == 600
    assert scene["screenType"] == "dome" and scene["stereoMode"] == "sbs" and scene["is3d"] is True
    source = scene["encodings"][0]["videoSources"][0]
    assert scene["encodings"][0]["name"] == "hevc", "DeoVR plays HEVC, so it is offered as it is"
    assert source["resolution"] == 2880
    assert source["url"].endswith(f"/word/video/{ids[VR]}.mp4?via=deovr"),         "links carry the word, the extension, and a mark saying the play came through DeoVR"
    assert source["url"].startswith("http://127.0.0.1"), \
        "always plain http: DeoVR refuses the certificate this PC signs for itself"


def test_the_folders_are_the_library(running):
    server, _db, ids = running
    status, _headers, body = fetch(server, "/word/deovr")
    assert status == 200
    feed = json.loads(body)
    assert feed["authorized"] == "1"
    names = [scene["name"] for scene in feed["scenes"]]
    assert "All videos" in names and "VR" in names
    assert names[0] in ("Recently added", "Unwatched", "VR", "Favorites", "All videos"),         "the views come before any performer"
    views = {"Recently added", "Unwatched", "VR", "Favorites", "All videos"}
    performer_tabs = [name for name in names if name not in views]
    assert performer_tabs == ["Anna B"], "performers are packed into tabs, named for who is in them"

    everything = next(scene for scene in feed["scenes"] if scene["name"] == "All videos")
    assert len(everything["list"]) == 2, "the hidden video is not offered"
    entry = everything["list"][0]
    assert set(entry) == {"title", "videoLength", "thumbnailUrl", "video_url"},         "short entries carry no id: a repeated one made DeoVR treat later tabs as already shown"
    assert "/word/deovr/video/" in entry["video_url"]

    # In a performers tab, each scene says who it belongs to, which is what the tab names were for.
    grouped = next(scene for scene in feed["scenes"] if scene["name"] == "Anna B")
    assert all(entry["title"].startswith("Anna Bell - ") for entry in grouped["list"])


def test_the_older_layouts_are_still_there_for_anyone_who_wants_them(running):
    """A tab per performer is available for a small library; so is nothing but the views."""
    server, db, _ids = running
    db.set_setting("deovr_layout", "performers")
    names = [scene["name"] for scene in json.loads(fetch(server, "/word/deovr")[2])["scenes"]]
    assert "Anna Bell" in names and "Anna B" not in names

    db.set_setting("deovr_layout", "simple")
    names = [scene["name"] for scene in json.loads(fetch(server, "/word/deovr")[2])["scenes"]]
    assert names == [n for n in names if n in ("Recently added", "Unwatched", "VR", "Favorites", "All videos")]

    db.set_setting("deovr_layout", "nonsense")  # a setting nobody recognises falls back
    names = [scene["name"] for scene in json.loads(fetch(server, "/word/deovr")[2])["scenes"]]
    assert "Anna B" in names


def test_a_scene_with_two_performers_appears_once_in_a_tab(running):
    server, db, ids = running
    db.set_performers(ids[VR], ["Anna Bell", "Amy Best"])   # both in the A-F tab
    db.commit()
    feed = json.loads(fetch(server, "/word/deovr")[2])
    tabs = [scene for scene in feed["scenes"]
            if scene["name"] not in ("Recently added", "Unwatched", "VR", "Favorites", "All videos")]
    wanted = f"/deovr/video/{ids[VR]}"
    assert sum(len([e for e in tab["list"] if e["video_url"].endswith(wanted)]) for tab in tabs) == 1


def test_a_hidden_video_is_not_served_even_by_asking_for_it(running):
    server, _db, ids = running
    status, _headers, _body = fetch(server, f"/word/deovr/video/{ids[SECRET]}")
    assert status == 404


# --------------------------------------------------------------------------- getting in

def test_without_the_word_deovr_is_asked_for_a_password(running):
    server, *_ = running
    status, _headers, body = fetch(server, "/deovr")
    assert status == 200, "an error DeoVR cannot explain would just look broken"
    assert json.loads(body) == {"scenes": [], "authorized": "0"}


def test_the_password_lets_deovr_in(running):
    server, *_ = running
    status, _headers, body = fetch(server, "/deovr", method="POST", body={"login": "anyone", "password": "word"})
    assert status == 200
    feed = json.loads(body)
    assert feed["authorized"] == "1" and feed["scenes"]

    status, _headers, body = fetch(server, "/deovr", method="POST", body={"login": "x", "password": "nope"})
    assert json.loads(body) == {"scenes": [], "authorized": "-1"}


def test_the_feed_works_without_https_because_players_are_not_browsers(running):
    """DeoVR will not take a certificate this PC signed for itself, so the feed answers plainly."""
    server, _db, ids = running
    status, _headers, body = fetch(server, "/word/deovr", secure=False)
    assert status == 200
    feed = json.loads(body)
    assert feed["authorized"] == "1"
    entry = feed["scenes"][0]["list"][0]
    assert entry["video_url"].startswith("http://127.0.0.1"), "links stay plain, whichever way DeoVR arrived"

    status, _headers, _body = fetch(server, f"/word/video/{ids[FLAT]}", secure=False)
    assert status == 200, "and the videos themselves play over plain http"

    status, headers, _body = fetch(server, "/word/", secure=False)
    assert status == 301 and headers["Location"].startswith("https://"), "the page still insists on https"


def test_a_passthrough_scene_is_flagged_where_deovr_looks_for_it():
    """DeoVR reads the alpha flag from the file name in the link, not from the feed or the title."""
    scene = {"id": 57, "path": "x.mp4", "title": "Kitchen", "tag_names": "Passthrough", "is_vr": True,
             "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 200}
    assert deovr.video_url(scene, "http://pc:8760/w") == "http://pc:8760/w/video/57_alpha.mp4?via=deovr"
    assert deovr.title_of(scene) == "Kitchen", "nothing is smuggled into the title"

    ordinary = {**scene, "tag_names": "POV"}
    assert deovr.video_url(ordinary, "http://pc:8760/w") == "http://pc:8760/w/video/57.mp4?via=deovr"


def test_a_link_with_words_in_it_still_finds_the_video(running):
    server, _db, ids = running
    status, _headers, body = fetch(server, f"/word/video/{ids[FLAT]}_alpha.mp4")
    assert status == 200 and body, "the id is the digits the name starts with"
