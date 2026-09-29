import http.client
import json
import os
import subprocess
import urllib.error
import urllib.request

import pytest

from blueshelf import paths, trash
from blueshelf.db import Database
from blueshelf.parser import parse_path
from blueshelf.server import RemoteServer

SCENE = "Site.24.03.05.Anna.Bell.Pool.Day.XXX.mp4"


@pytest.fixture()
def running(tmp_path, monkeypatch):
    data = tmp_path / "data"
    library = tmp_path / "library"
    (data / "thumbs").mkdir(parents=True)
    library.mkdir()
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(data))
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "thumbs_dir", lambda: data / "thumbs")
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")

    video_path = library / SCENE
    video_path.write_bytes(bytes(range(256)) * 40)  # 10 240 bytes of predictable content
    db = Database(str(data / "library.db"))
    lib_id = db.add_library(str(library))
    video_id = db.insert_video(lib_id, str(video_path), SCENE, video_path.stat().st_size, 1.0,
                               parse_path(SCENE), True)
    db.set_fields(video_id, duration=120.0, width=3840, height=1920, is_vr=1, thumb=f"{video_id}.jpg")
    (data / "thumbs" / f"{video_id}.jpg").write_bytes(b"jpegdata")

    server = RemoteServer(token="secret", port=0, secure=False)
    server.start()
    yield server, db, video_id, video_path
    server.stop()
    db.close()


def fetch(server, route, token="secret", method="GET", payload=None, headers=None):
    url = f"http://127.0.0.1:{server.port}{route}"
    if token:
        url += ("&" if "?" in route else "?") + f"k={token}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.headers, response.read()


def test_the_passcode_is_required(running):
    server, *_ = running
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(server, "/api/library", token="")
    assert error.value.code == 401
    with pytest.raises(urllib.error.HTTPError):
        fetch(server, "/api/library", token="wrong")


def test_library_lists_videos_with_their_vr_details(running):
    server, _db, video_id, _path = running
    status, _headers, body = fetch(server, "/api/library")
    assert status == 200
    videos = json.loads(body)
    assert [v["id"] for v in videos] == [video_id]
    scene = videos[0]
    assert scene["title"] == "Pool Day"
    assert scene["performers"] == ["Anna Bell"]
    assert scene["is_vr"] is True and scene["vr_layout"] == 1 and scene["vr_proj"] == 0
    assert scene["video"] == f"/video/{video_id}"
    assert scene["thumb"] == f"/thumb/{video_id}.jpg"


def test_performers_endpoint(running):
    server, *_ = running
    _status, _headers, body = fetch(server, "/api/performers")
    assert [(p["name"], p["count"]) for p in json.loads(body)] == [("Anna Bell", 1)]


def test_video_streams_with_range_requests(running):
    server, _db, video_id, path = running
    contents = path.read_bytes()
    status, headers, body = fetch(server, f"/video/{video_id}", headers={"Range": "bytes=100-199"})
    assert status == 206
    assert headers["Content-Range"] == f"bytes 100-199/{len(contents)}"
    assert body == contents[100:200]
    status, headers, body = fetch(server, f"/video/{video_id}")
    assert status == 200 and len(body) == len(contents)
    assert headers["Accept-Ranges"] == "bytes"


def test_conversion_streams_playable_mp4(running, tmp_path):
    """Devices that cannot decode the original get a converted H.264 stream instead."""
    server, db, _video_id, _path = running
    clip = tmp_path / "clip.mp4"
    subprocess.run([paths.tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=15",
                    "-t", "2", "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True, timeout=120)
    library_id = db.libraries()[0]["id"]
    clip_id = db.insert_video(library_id, str(clip), clip.name, clip.stat().st_size, 1.0, parse_path(clip.name),
                              True)
    db.commit()

    url = f"http://127.0.0.1:{server.port}/convert/{clip_id}?q=low&k=secret"
    with urllib.request.urlopen(url, timeout=90) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "video/mp4"
        head = response.read(32768)
    assert b"ftyp" in head[:64], "should start with an MP4 header"


def test_conversion_of_a_missing_video(running):
    server, *_ = running
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(server, "/convert/999?q=low")
    assert error.value.code == 404


def test_library_says_what_each_file_is(running):
    server, *_ = running
    scene = json.loads(fetch(server, "/api/library")[2])[0]
    assert scene["container"] == "mp4"
    assert "convert" in scene and scene["convert"].startswith("/convert/")


def test_thumbnail_is_served(running):
    server, _db, video_id, _path = running
    status, headers, body = fetch(server, f"/thumb/{video_id}.jpg")
    assert status == 200 and body == b"jpegdata"
    assert headers["Content-Type"] == "image/jpeg"


def test_deleting_from_the_headset_moves_the_file_to_the_trash(running):
    server, db, video_id, path = running
    status, _headers, body = fetch(server, f"/api/video/{video_id}/delete", method="POST", payload={})
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert not path.exists()
    assert db.get_video(video_id)["trashed"] == 1
    library = os.path.dirname(str(path))
    assert len(trash.trash_contents(library)) == 1
    assert json.loads(fetch(server, "/api/library")[2]) == []


def test_favorite_and_rating_come_back_to_the_desktop(running):
    server, db, video_id, _path = running
    fetch(server, f"/api/video/{video_id}/favorite", method="POST", payload={"value": True})
    fetch(server, f"/api/video/{video_id}/rating", method="POST", payload={"value": 4})
    fetch(server, f"/api/video/{video_id}/resume", method="POST", payload={"value": 61.5})
    video = db.get_video(video_id)
    assert video["favorite"] == 1 and video["rating"] == 4 and video["resume_pos"] == 61.5


def test_missing_video_and_unknown_routes(running):
    server, *_ = running
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(server, "/api/video/999")
    assert error.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        fetch(server, "/api/nonsense")


def test_addresses_are_usable_and_not_repeated(running):
    server, *_ = running
    addresses = server.addresses()
    assert addresses
    assert len(addresses) == len(set(addresses))
    assert all(address.startswith("http://") and address.endswith(f":{server.port}") for address in addresses)


def test_the_address_word_works_as_the_passcode(running):
    """http://pc:8760/secret is easier to type in a headset than a query string."""
    server, *_ = running
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)

    def get(route, cookie=None):
        connection.request("GET", route, headers={"Cookie": cookie} if cookie else {})
        response = connection.getresponse()
        body = response.read()
        return response, body

    response, _body = get("/secret")
    assert response.status == 302
    assert response.getheader("Location") == "/secret/", "the word must stay in the address"

    # From here on nothing sends a cookie: a headset browser that drops it must still work.
    response, body = get("/secret/")
    assert response.status == 200 and b"<title>BlueShelf</title>" in body
    for asset in ("/secret/style.css", "/secret/app.js", "/secret/three.module.min.js"):
        response, body = get(asset)
        assert response.status == 200 and body, asset
    response, body = get("/secret/api/library")
    assert response.status == 200 and json.loads(body)

    response, _body = get("/wrongword/api/library")
    assert response.status == 401
    connection.close()


def test_the_word_also_leaves_a_cookie_so_plain_addresses_work(running):
    """Anything that asks for a bare address still gets in if the browser kept the cookie."""
    server, *_ = running
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("GET", "/secret/")
    response = connection.getresponse()
    response.read()
    assert "blueshelf_key=secret" in (response.getheader("Set-Cookie") or "")
    connection.request("GET", "/api/library", headers={"Cookie": "blueshelf_key=secret"})
    response = connection.getresponse()
    assert response.status == 200 and json.loads(response.read())
    connection.close()


def test_the_web_page_and_its_files_are_served(running):
    server, *_ = running
    status, headers, body = fetch(server, "/")
    assert status == 200 and b"<title>BlueShelf</title>" in body
    assert headers["Content-Type"] == "text/html"
    assert "blueshelf_key=secret" in (headers["Set-Cookie"] or "")
    for asset in ("/app.js", "/style.css", "/three.module.min.js"):
        status, _headers, body = fetch(server, asset)
        assert status == 200 and body


def test_static_files_cannot_escape_the_web_folder(running):
    server, *_ = running
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(server, "/../db.py")
    assert error.value.code == 404


def test_an_address_without_the_word_explains_itself(running):
    """A person typing just the IP should read what to do, not a line of JSON."""
    server, *_ = running
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("GET", "/")
    response = connection.getresponse()
    body = response.read()
    assert response.status == 401
    assert response.getheader("Content-Type").startswith("text/html")
    assert b"yourword" in body and b"secret" not in body, "must not give the word away"
    connection.close()


def test_the_page_is_served_over_https_so_the_headset_offers_vr(tmp_path, monkeypatch):
    """WebXR does not exist on a plain http page, so the headset would have no VR mode at all."""
    import ssl

    data = tmp_path / "secure"
    (data / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    monkeypatch.setattr(paths, "thumbs_dir", lambda: data / "thumbs")
    Database(str(data / "library.db")).close()

    server = RemoteServer(token="secret", port=0)
    server.start()
    try:
        assert server.scheme == "https"
        assert all(address.startswith("https://") for address in server.addresses())
        relaxed = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # the certificate signs itself
        relaxed.check_hostname = False
        relaxed.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", server.port, context=relaxed, timeout=10)
        connection.request("GET", "/secret/")
        response = connection.getresponse()
        assert response.status == 200 and b"<title>BlueShelf</title>" in response.read()
        connection.close()

        # Nobody types "https://" into a headset, so plain http on the same port points the way.
        plain = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        plain.request("GET", "/secret/")
        response = plain.getresponse()
        response.read()
        assert response.status == 301
        assert response.getheader("Location").startswith("https://")
        assert response.getheader("Location").endswith("/secret/")
        plain.close()
    finally:
        server.stop()


def test_the_certificate_covers_this_pc(tmp_path, monkeypatch):
    from blueshelf import certs

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    pair = certs.ensure()
    assert pair, "a certificate should be made on a normal PC"
    first = (tmp_path / "headset-cert.pem").read_bytes()
    assert certs.ensure() == pair
    assert (tmp_path / "headset-cert.pem").read_bytes() == first, "should not be remade every time"


def test_nothing_gets_through_without_the_word(running):
    """The files themselves have to be as closed as the pages are."""
    server, _db, video_id, path = running
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)

    def refused(route, method="GET"):
        connection.request(method, route)
        response = connection.getresponse()
        response.read()
        return response.status

    assert refused(f"/video/{video_id}") == 401
    assert refused(f"/convert/{video_id}?q=low") == 401
    assert refused(f"/thumb/{video_id}.jpg") == 401
    assert refused(f"/api/video/{video_id}/delete", "POST") == 401
    assert refused("/api/library?k=nearly-secret") == 401
    assert path.exists(), "a refused delete must leave the file alone"

    # ...while every one of them works through the word.
    for route in (f"/secret/video/{video_id}", f"/secret/thumb/{video_id}.jpg", "/secret/api/library"):
        connection.request("GET", route)
        response = connection.getresponse()
        response.read()
        assert response.status == 200, route
    connection.close()


def test_a_play_through_deovr_is_counted_once_not_once_per_request(running):
    """Players ask for a file many times over: seeking, buffering, reading the index at the end."""
    server, db, video_id, _path = running
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)

    def ask(route, headers=None):
        connection.request("GET", route, headers=headers or {})
        response = connection.getresponse()
        response.read()
        return response.status

    assert ask(f"/secret/video/{video_id}.mp4?via=deovr") == 200
    assert ask(f"/secret/video/{video_id}.mp4?via=deovr", {"Range": "bytes=500-999"}) == 206
    assert ask(f"/secret/video/{video_id}.mp4?via=deovr", {"Range": "bytes=0-99"}) == 206
    connection.close()

    assert db.play_counts(video_id) == {"deovr": 1}, "one sitting is one play"
    assert db.get_video(video_id)["play_count"] == 1


def test_the_page_and_deovr_are_told_apart(running):
    server, db, video_id, _path = running
    from blueshelf import server as server_module
    server_module._counted.clear()

    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("GET", f"/secret/video/{video_id}")   # the page asks without a marker
    connection.getresponse().read()
    connection.close()
    assert db.play_counts(video_id) == {"page": 1}


def test_a_mask_beside_a_film_is_found_and_a_listing_does_not_look(tmp_path):
    """The player can lay a separate cut-out over a film instead of it being written in.

    Looking for one means asking the disk, so a library listing must not do it for every
    video it returns - over a network that is thousands of round trips nobody asked for.
    """
    from blueshelf.server import mask_beside

    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    assert mask_beside(str(video)) == "", "nothing there yet"

    beside = tmp_path / "scene.mask.mp4"
    beside.write_bytes(b"m")
    assert mask_beside(str(video)) == str(beside), "found next to the film"

    beside.unlink()
    folder = tmp_path / ".scene.matte"
    folder.mkdir()
    inside = folder / "mask.mp4"
    inside.write_bytes(b"m")
    assert mask_beside(str(video)) == str(inside), "or in the working folder"


def test_a_viewer_only_ever_has_one_conversion_running():
    """Seeking in a converted stream starts a new conversion from the new spot. The old one
    used to run on to the end of the film unless the headset closed its connection, which
    it often does not - six were found at once, all decoding 7K on the graphics card."""
    import subprocess as sp
    import sys
    from blueshelf import server

    def sleeper():
        return sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    first, second, elsewhere = sleeper(), sleeper(), sleeper()
    try:
        server.take_over_conversion("192.168.1.50", first)
        server.take_over_conversion("192.168.1.77", elsewhere)
        server.take_over_conversion("192.168.1.50", second)     # the same headset seeks

        assert first.wait(timeout=10) is not None, "the old conversion should be stopped"
        assert second.poll() is None, "the new one keeps going"
        assert elsewhere.poll() is None, "and another viewer's is left alone"

        server.let_go_of_conversion("192.168.1.50", first)       # a late finish of the old one
        assert server._conversions.get("192.168.1.50") is second, "must not forget the new one"
    finally:
        for proc in (first, second, elsewhere):
            proc.kill()
        server.let_go_of_conversion("192.168.1.50", second)
        server.let_go_of_conversion("192.168.1.77", elsewhere)


def test_a_film_can_have_several_named_cut_outs(tmp_path):
    """One from each matting model, say, so the best-looking can be picked while watching."""
    from blueshelf.server import mask_beside, masks_beside

    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    work = tmp_path / ".scene.matte"
    work.mkdir()
    (work / "mask-resnet50.mp4").write_bytes(b"r")
    (work / "mask-matanyone2.mp4").write_bytes(b"m")
    (tmp_path / "scene.mask.mp4").write_bytes(b"c")

    names = [name for name, _path in masks_beside(str(video))]
    assert names == ["cut-out", "matanyone2", "resnet50"], names
    assert mask_beside(str(video), "matanyone2").endswith("mask-matanyone2.mp4")
    assert mask_beside(str(video), "nonexistent") == "", "only names that are really there"
    assert mask_beside(str(video), "../../windows/win.ini") == "", "never an arbitrary path"
