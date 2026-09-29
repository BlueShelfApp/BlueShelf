"""Drives the headset page in a real browser, because the VR view can only be judged by looking.

Skipped when Chrome or the devtools client is missing, so the rest of the suite still runs anywhere.
"""
from __future__ import annotations

import json
import pathlib
import os
import shutil
import subprocess
import time
import urllib.request

import pytest

from blueshelf import paths
from blueshelf.db import Database
from blueshelf.parser import parse_path
from blueshelf.server import RemoteServer

websocket = pytest.importorskip("websocket", reason="needs websocket-client")

CHROME_PLACES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME = next((path for path in CHROME_PLACES if os.path.isfile(path)), shutil.which("chrome"))
pytestmark = pytest.mark.skipif(not CHROME, reason="needs Google Chrome")

# No fixed port. One was used for a long time and every abandoned Chrome went on holding
# it, so a later run could not bind and failed with "Chrome did not start" - sixteen
# orphans had piled up, the oldest eleven hours old. Chrome picks its own free port when
# asked for 0 and writes it into DevToolsActivePort in its profile folder.


class Browser:
    """Just enough of the devtools protocol to open a page, click things and read values back."""

    def __init__(self, profile: str):
        self.profile = pathlib.Path(profile)
        self.process = subprocess.Popen(
            [CHROME, "--headless=new", "--remote-debugging-port=0", "--remote-allow-origins=*",
             f"--user-data-dir={profile}", "--no-first-run", "--window-size=1000,700",
             "--autoplay-policy=no-user-gesture-required", "--enable-unsafe-swiftshader",
             "--ignore-certificate-errors", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.port = self._wait_for_port()
        page = self._wait_for_page()
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        self.counter = 0
        self.send("Page.enable")
        self.send("Runtime.enable")

    def _wait_for_port(self) -> int:
        """The port Chrome actually chose, which it writes out once it is listening."""
        told = self.profile / "DevToolsActivePort"
        for _ in range(60):
            try:
                first = told.read_text(encoding="utf-8").splitlines()[0].strip()
                if first.isdigit():
                    return int(first)
            except (OSError, IndexError):
                pass
            time.sleep(0.5)
        raise RuntimeError("Chrome did not start")

    def _wait_for_page(self):
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=2) as response:
                    pages = [t for t in json.load(response) if t["type"] == "page"]
                    if pages:
                        return pages[0]
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("Chrome did not start")

    def send(self, method, **params):
        self.counter += 1
        self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.counter:
                return message.get("result", {})

    def ask(self, expression):
        result = self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"].get("exception", {}).get("description"))
        return result.get("result", {}).get("value")

    def go(self, url, settle=4.0):
        self.send("Page.navigate", url=url)
        time.sleep(settle)

    def close(self):
        try:
            self.ws.close()
        finally:
            # Chrome's children are not this process's to Windows once the launcher goes,
            # so terminate() alone left them running and holding their port.
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                               capture_output=True)
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()


@pytest.fixture()
def page(tmp_path, monkeypatch):
    data, library = tmp_path / "data", tmp_path / "library"
    (data / "thumbs").mkdir(parents=True)
    library.mkdir()
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(data))
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "thumbs_dir", lambda: data / "thumbs")
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")

    # A side-by-side 180 video: left eye red, right eye blue, each with a white cross, so a swapped
    # eye or a squashed picture is obvious in a single pixel.
    clip = library / "TestVR.Anna.Bell.180_LR.mp4"
    made = subprocess.run(
        [paths.tool("ffmpeg"), "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=640x640:r=10:d=6",
         "-f", "lavfi", "-i", "color=c=blue:s=640x640:r=10:d=6",
         "-filter_complex",
         "[0:v]drawbox=x=300:y=0:w=40:h=640:color=white:t=fill[l];"
         "[1:v]drawbox=x=300:y=0:w=40:h=640:color=white:t=fill[r];[l][r]hstack",
         "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-t", "6", str(clip)],
        capture_output=True, timeout=180)
    if made.returncode != 0:
        pytest.skip("no H.264 encoder here to make the test clip")

    db = Database(str(data / "library.db"))
    library_id = db.add_library(str(library))
    video_id = db.insert_video(library_id, str(clip), clip.name, clip.stat().st_size, 1.0,
                               parse_path(clip.name), True)
    db.set_fields(video_id, duration=6.0, width=1280, height=640, is_vr=1, vr_layout=1, vr_proj=0)
    db.commit()

    server = RemoteServer(token="word", port=0, secure=False)
    server.start()
    browser = Browser(str(tmp_path / "profile"))
    yield browser, server
    browser.close()
    server.stop()
    db.close()


def open_the_video(browser, server):
    browser.go(f"http://127.0.0.1:{server.port}/word/?debug")
    assert browser.ask("document.querySelectorAll('.card').length") == 1
    browser.ask("document.querySelector('.card').click()")
    time.sleep(5)


def _mask_beside_the_clip(tmp_path):
    """A small separate mask next to the test film: left half kept, right half cut away."""
    clip = tmp_path / "library" / "TestVR.Anna.Bell.180_LR.mp4"
    mask = clip.with_name(clip.stem + ".mask.mp4")
    subprocess.run(
        [paths.tool("ffmpeg"), "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=white:s=128x128:r=10:d=6",
         "-f", "lavfi", "-i", "color=c=black:s=128x128:r=10:d=6",
         "-filter_complex", "[0:v][1:v]hstack", "-pix_fmt", "yuv420p",
         "-c:v", "h264_nvenc", "-t", "6", str(mask)],
        capture_output=True, timeout=180, check=True)
    return mask


def test_a_mask_kept_beside_the_film_is_played_with_it(page, tmp_path):
    """A cut-out can sit next to the original instead of being written into a new copy of it.

    That saves hours of re-encoding and tens of gigabytes a film, but only if the player finds
    it, uses it, and keeps it in step - a mask a few frames behind trails a ghost."""
    browser, server = page
    _mask_beside_the_clip(tmp_path)
    open_the_video(browser, server)

    found = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        const mesh = d.vr.meshes[0];
        return {
          src: d.vr.maskVideo ? d.vr.maskVideo.src : "",
          separate: mesh ? mesh.material.uniforms.separate.value : -1,
          mode: d.vr.see.mode,
        };
      })()
    """)
    assert "/mask/" in found["src"], "the mask beside the film should have been loaded"
    assert "m=cut-out" in found["src"], "asked for by name, so the right one of several comes"
    assert found["separate"] == 1, "the picture should be cut out by the separate mask"
    assert found["mode"] == 1, "and it should start with the cut-out showing"

    browser.ask("window.blueshelfDebug.video.play(), true")
    time.sleep(2.5)
    playing = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        return Math.abs(d.vr.maskVideo.currentTime - (d.state.offset + d.video.currentTime));
      })()
    """)
    assert playing < 0.35, f"the mask drifted {playing:.2f}s from the film while playing"

    browser.ask("window.blueshelfDebug.video.pause(), window.blueshelfDebug.video.currentTime = 4.0, true")
    time.sleep(2.0)
    parked = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        return Math.abs(d.vr.maskVideo.currentTime - (d.state.offset + d.video.currentTime));
      })()
    """)
    assert parked < 0.15, f"after a seek the mask was left {parked:.2f}s away from the film"


def test_a_mask_that_will_not_play_leaves_the_film_whole(page, tmp_path):
    """A mask the headset cannot decode used to be read as "nothing here", cutting the whole
    picture away - a black screen, with the only explanation in a message nobody inside a
    headset can see. It must fall back to the film, and say so where it can be read."""
    browser, server = page
    clip = tmp_path / "library" / "TestVR.Anna.Bell.180_LR.mp4"
    clip.with_name(clip.stem + ".mask.mp4").write_bytes(b"this is not a video" * 500)
    open_the_video(browser, server)
    time.sleep(3)

    state = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        const mesh = d.vr.meshes[0];
        return { gone: d.vr.maskVideo === null, why: d.vr.maskState,
                 separate: mesh.material.uniforms.separate.value,
                 left: document.querySelectorAll('.mask-source').length };
      })()
    """)
    assert state["gone"], "a mask that failed should be let go of"
    assert state["why"] == "failed", "and the panel should be able to say so"
    assert state["separate"] == 0, "the picture must not be cut out by a mask that is not there"
    assert state["left"] == 0, "nor left behind in the page"


def test_a_film_without_a_mask_does_not_pretend_to_have_one(page):
    browser, server = page
    open_the_video(browser, server)
    assert browser.ask("window.blueshelfDebug.vr.maskVideo === null")
    assert browser.ask("window.blueshelfDebug.vr.meshes[0].material.uniforms.separate.value") == 0


def test_a_vr_video_is_shown_on_a_sphere_not_cropped(page):
    """Cropping half a side-by-side frame in the page never looks right, so it goes on a sphere."""
    browser, server = page
    open_the_video(browser, server)
    assert browser.ask("!document.getElementById('player').hidden")
    assert browser.ask("!document.getElementById('vr-stage').hidden"), "the sphere should be showing"
    assert browser.ask("document.getElementById('video').classList.contains('as-source')"), \
        "the video element stays as the picture source"
    assert browser.ask("document.querySelector('#vr-stage canvas').width") > 100
    assert browser.ask("document.getElementById('video').readyState") >= 2, "the video should be playing"


def test_each_display_gets_its_own_eye(page):
    """In the headset the left display must get the left half of the frame, and the right the right."""
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.buildScene(d.state.current, true);
        const read = (layer) => {
          d.vr.camera.layers.set(layer);
          d.vr.camera.rotation.set(0, 0, 0);
          d.vr.renderer.render(d.vr.scene, d.vr.camera);
          const gl = d.vr.renderer.getContext();
          const pixel = new Uint8Array(4);
          gl.readPixels(Math.floor(gl.drawingBufferWidth * 0.12), Math.floor(gl.drawingBufferHeight * 0.18),
                        1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
          return [pixel[0], pixel[1], pixel[2]];
        };
        const out = { masks: d.vr.meshes.map((m) => m.layers.mask), left: read(1), right: read(2) };
        d.vr.camera.layers.set(0);
        return out;
      })()
    """)
    assert result["masks"] == [2, 4], "layer 1 goes to the left display, layer 2 to the right"
    assert result["left"][0] > 200 and result["left"][2] < 60, f"left display should be red, got {result['left']}"
    assert result["right"][2] > 200 and result["right"][0] < 60, f"right display should be blue, got {result['right']}"


def test_the_film_keeps_its_own_brightness_and_contrast(page, tmp_path):
    """The sphere's shader read the film's colours still sRGB-encoded and encoded them again
    on the way out: blacks lifted, contrast flattened, washed out beside the same file in
    DeoVR. Black and white survive that unchanged, which is why the red and blue test clip
    never showed it - greys do not."""
    browser, server = page
    clip = tmp_path / "library" / "TestVR.Anna.Bell.180_LR.mp4"
    made = subprocess.run(
        [paths.tool("ffmpeg"), "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=0x404040:s=640x640:r=10:d=6",
         "-f", "lavfi", "-i", "color=c=0xB0B0B0:s=640x640:r=10:d=6",
         "-filter_complex", "[0:v][1:v]hstack", "-pix_fmt", "yuv420p",
         "-c:v", "h264_nvenc", "-t", "6", str(clip)],
        capture_output=True, timeout=180)
    assert made.returncode == 0, made.stderr
    open_the_video(browser, server)
    shown = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.buildScene(d.state.current, true);
        const read = (layer) => {
          d.vr.camera.layers.set(layer);
          d.vr.camera.rotation.set(0, 0, 0);
          d.vr.renderer.render(d.vr.scene, d.vr.camera);
          const gl = d.vr.renderer.getContext();
          const pixel = new Uint8Array(4);
          gl.readPixels(Math.floor(gl.drawingBufferWidth / 2), Math.floor(gl.drawingBufferHeight / 2),
                        1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
          return pixel[1];
        };
        const out = { dark: read(1), light: read(2) };
        d.vr.camera.layers.set(0);
        return out;
      })()
    """)
    assert abs(shown["dark"] - 0x40) <= 8, f"a dark grey of 64 came out {shown['dark']}"
    assert abs(shown["light"] - 0xB0) <= 8, f"a light grey of 176 came out {shown['light']}"


@pytest.mark.parametrize("fov", [180, 190])
def test_a_fisheye_film_is_not_drawn_mirrored(page, tmp_path, fov):
    """What is on your right has to come from the right of the fisheye circle.

    It came from the left. One eye alone just looks mirrored, but both eyes mirrored reverse
    the difference between them, and the two pictures will not fuse: the correct lens showed
    a double image, while the wrong one (flat 180, mapped the right way round) looked whole.
    """
    browser, server = page
    clip = tmp_path / "library" / "TestVR.Anna.Bell.180_LR.mp4"
    # Each eye: red on its left half, green on its right, a white band across its top.
    made = subprocess.run(
        [paths.tool("ffmpeg"), "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=320x640:r=10:d=6",
         "-f", "lavfi", "-i", "color=c=0x00C000:s=320x640:r=10:d=6",
         "-filter_complex",
         "[0:v][1:v]hstack,drawbox=x=0:y=0:w=640:h=150:color=white:t=fill,split[a][b];"
         "[a][b]hstack",
         "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-t", "6", str(clip)],
        capture_output=True, timeout=180)
    assert made.returncode == 0, made.stderr
    open_the_video(browser, server)
    seen = browser.ask(f"""
      (() => {{
        const d = window.blueshelfDebug;
        d.state.current.vr_proj = 2;
        d.state.current.vr_lens_fov = {fov};
        d.buildScene(d.state.current, true);
        const look = (layer, yaw, pitch) => {{
          d.vr.camera.layers.set(layer);
          d.vr.camera.rotation.set(pitch * Math.PI / 180, yaw * Math.PI / 180, 0);
          d.vr.camera.updateMatrixWorld(true);
          d.vr.renderer.render(d.vr.scene, d.vr.camera);
          const gl = d.vr.renderer.getContext();
          const p = new Uint8Array(4);
          gl.readPixels(Math.floor(gl.drawingBufferWidth / 2), Math.floor(gl.drawingBufferHeight / 2),
                        1, 1, gl.RGBA, gl.UNSIGNED_BYTE, p);
          return p[0] > 150 && p[1] > 150 ? "white" : p[0] > 150 ? "red" : p[1] > 100 ? "green" : "dark";
        }};
        const out = {{}};
        for (const [layer, eye] of [[1, "left"], [2, "right"]]) {{
          out[eye] = {{ lookingLeft: look(layer, 40, 0), lookingRight: look(layer, -40, 0),
                       lookingUp: look(layer, 0, 70) }};
        }}
        d.vr.camera.rotation.set(0, 0, 0);
        d.vr.camera.layers.set(0);
        return out;
      }})()
    """)
    for eye in ("left", "right"):
        assert seen[eye]["lookingLeft"] == "red", f"{eye} eye, looking left: {seen[eye]}"
        assert seen[eye]["lookingRight"] == "green", f"{eye} eye, looking right: {seen[eye]}"
        assert seen[eye]["lookingUp"] == "white", f"{eye} eye, looking up: {seen[eye]}"


def test_the_headset_draws_at_full_resolution_everywhere(page):
    """three.js asks for less than the panels' own resolution and blurs everything away from
    the middle of the view. Fine for a game; not for watching a film."""
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        const xr = d.vr.renderer.xr;
        const calls = [];
        const keep = [xr.setFramebufferScaleFactor, xr.setFoveation, window.XRWebGLLayer];
        xr.setFramebufferScaleFactor = (s) => calls.push(["scale", s]);
        xr.setFoveation = (f) => calls.push(["foveation", f]);
        const says = (value) => { window.XRWebGLLayer = { getNativeFramebufferScaleFactor: value }; };
        says(() => 1.35);
        const quest = d.sharpen({});
        says(() => 4);
        const greedy = d.sharpen({});
        says(() => { throw new Error("not offered"); });
        const unknown = d.sharpen({});
        [xr.setFramebufferScaleFactor, xr.setFoveation, window.XRWebGLLayer] = keep;
        return { quest, greedy, unknown, calls, cap: d.SHARPEST };
      })()
    """)
    assert result["quest"] == 1.35, "the panels' own resolution"
    assert result["greedy"] == result["cap"], "but never more than the cap"
    assert result["unknown"] == 1, "and the default when the headset will not say"
    assert ["foveation", 0] in result["calls"], "nothing blurred at the edges"
    assert result["calls"].count(["foveation", 0]) == 3
