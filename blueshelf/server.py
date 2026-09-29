"""A small web server so the library can be browsed, watched and tidied from a headset or phone.

The Quest browser opens the page, so nothing has to be installed on the headset. Videos stream with
range requests so seeking works, and deleting goes through the same trash as the desktop app, so a
mistaken tap in the headset can still be undone on the PC.

The server is off unless it is switched on, listens only on the local network, and every request
needs the passcode.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from blueshelf import certs, deovr, media, paths, trash
from blueshelf.db import Database

DEFAULT_PORT = 8760
CHUNK = 256 * 1024
WEB_DIR = paths.PACKAGE_DIR / "web"
# An address word that matched one of these would swallow the page's own addresses.
RESERVED_WORDS = {"api", "video", "convert", "thumb", "mask", "strip", "fonts", "index.html",
                  "app.js", "style.css", "icons.js", "shelf.js", "shelf-vr.js", "worlds.js"}
# Windows does not always know the web font type, and a font sent as "octet-stream" is one
# some browsers refuse.
mimetypes.add_type("font/woff2", ".woff2")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")

# One live conversion per viewer. Seeking in a converted stream asks for a fresh conversion
# from the new spot, and the old one was only stopped if the headset closed its connection -
# which a headset's browser does not reliably do; it can simply stop reading. The old ones
# ran on to the end of the film, each decoding 7K on the graphics card: six were found at
# once, starving each other and making the lower qualities play worse than the original.
_conversions: dict[str, subprocess.Popen] = {}
_conversions_lock = threading.Lock()
# A viewer that has not taken any data for this long has gone, whatever its connection says.
STALLED_VIEWER = 45


def take_over_conversion(viewer: str, process: subprocess.Popen) -> None:
    """Make this the viewer's only conversion, stopping whatever it had before."""
    with _conversions_lock:
        old = _conversions.get(viewer)
        _conversions[viewer] = process
    if old is not None and old is not process and old.poll() is None:
        try:
            old.kill()
        except OSError:
            pass


def let_go_of_conversion(viewer: str, process: subprocess.Popen) -> None:
    with _conversions_lock:
        if _conversions.get(viewer) is process:
            del _conversions[viewer]

# Headsets and phones often cannot decode what a PC plays happily: HEVC is patchy in browsers and
# 8K is beyond most mobile decoders. These convert on the fly to plain H.264 instead.
QUALITIES = {
    "high": {"width": 3840, "bitrate": "14M", "label": "Converted, high"},
    "medium": {"width": 2560, "bitrate": "7M", "label": "Converted, medium"},
    "low": {"width": 1600, "bitrate": "3M", "label": "Converted, low"},
}
_encoder_cache: list[str] = []

# Fastest first. Being listed by FFmpeg does not mean an encoder works: NVENC is listed on machines
# whose driver is too old for it, so each candidate is tried for real before it is used.
ENCODER_CANDIDATES = [
    ["-c:v", "h264_nvenc", "-preset", "p4"],
    ["-c:v", "h264_qsv", "-preset", "veryfast"],
    ["-c:v", "h264_amf", "-quality", "speed"],
    ["-c:v", "h264_mf"],
    ["-c:v", "libopenh264"],
    ["-c:v", "libx264", "-preset", "veryfast"],
]


def video_encoder() -> list[str]:
    """The fastest H.264 encoder this machine can actually run."""
    if _encoder_cache:
        return _encoder_cache
    for candidate in ENCODER_CANDIDATES:
        try:
            # 320x240 because hardware encoders reject tiny frames: NVENC refuses anything smaller
            # than about 145x49, which made an earlier 64x64 probe discard encoders that work.
            result = paths.run_tool([paths.tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
                                     "-f", "lavfi", "-i", "testsrc2=s=320x240:r=10", "-t", "0.3",
                                     *candidate, "-f", "null", "-"], timeout=60)
        except Exception:
            continue
        if result.returncode == 0:
            _encoder_cache.extend(candidate)
            return _encoder_cache
    _encoder_cache.extend(["-c:v", "libopenh264"])  # last resort, so there is always a command to run
    return _encoder_cache


def local_addresses(port: int, scheme: str = "http") -> list[str]:
    """The addresses this machine can be reached at, the one that really works first.

    A PC usually has extra adapters (WSL, Hyper-V, Docker, VPNs) whose addresses a headset cannot
    reach, so the address of the interface holding the default route is put at the top.
    """
    addresses: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # a reserved address, so nothing is actually sent
        addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in addresses:
                addresses.append(ip)
    except OSError:
        pass
    if not addresses:
        addresses.append("127.0.0.1")
    return [f"{scheme}://{ip}:{port}" for ip in addresses]


def make_token() -> str:
    return secrets.token_urlsafe(6)


# A player asks for a video many times over - seeking, buffering, reading the index at the end -
# so a play is counted once per video, per device, per half hour, rather than once per request.
_counted: dict[tuple, float] = {}
_counted_lock = threading.Lock()
PLAY_GAP = 30 * 60


def count_play(db, video_id: int, who: str, source: str) -> bool:
    """True when this counted as a new play rather than more of the same one."""
    now = time.time()
    with _counted_lock:
        key = (video_id, who, source)
        last = _counted.get(key, 0)
        if now - last < PLAY_GAP:
            return False
        _counted[key] = now
        for old in [k for k, when in _counted.items() if now - when > PLAY_GAP * 4]:
            _counted.pop(old, None)
    db.record_play(video_id, source)
    return True


MASK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,30}$")


def masks_beside(video_path: str) -> list[tuple[str, str]]:
    """The separate cut-out masks kept for this video, as (name, path), plainest first.

    Found by where they sit rather than recorded anywhere, so a mask can simply be dropped
    next to a film. Unlike a packed copy this leaves the original untouched and costs a
    gigabyte or so instead of tens, because the player lays the two together as it draws.
    Nothing but this player understands it, so a packed copy is still what you make for
    DeoVR or a headset's own app.

    Several can sit side by side - one from each matting model, say - so the one that
    looks best can be picked while watching:

        film.mask.mp4                  "cut-out"
        film.mask.matanyone2.mp4       "matanyone2"
        .film.matte/mask.mp4           "cut-out"
        .film.matte/mask-resnet50.mp4  "resnet50"
    """
    here = Path(video_path)
    work = here.parent / f".{here.stem}.matte"
    found: dict[str, str] = {}

    def keep(name: str, path: Path) -> None:
        if name not in found and MASK_NAME.match(name):
            found[name] = str(path)

    try:
        if here.with_name(f"{here.stem}.mask.mp4").is_file():
            keep("cut-out", here.with_name(f"{here.stem}.mask.mp4"))
        for other in sorted(here.parent.glob(f"{here.stem}.mask.*.mp4")):
            keep(other.name[len(here.stem) + len(".mask."):-len(".mp4")], other)
    except OSError:
        pass
    try:
        if work.is_dir():
            if (work / "mask.mp4").is_file():
                keep("cut-out", work / "mask.mp4")
            for other in sorted(work.glob("mask-*.mp4")):
                keep(other.stem[len("mask-"):], other)
    except OSError:
        pass
    return list(found.items())


def mask_beside(video_path: str, name: str = "") -> str:
    """The path of one named mask, the plainest one if no name is given, or ""."""
    masks = masks_beside(video_path)
    if name:
        return next((path for label, path in masks if label == name), "")
    return masks[0][1] if masks else ""


def _video_id(route: str) -> int:
    """The number in /video/12, /video/12.mp4 or /video/12_alpha.mp4.

    Players read things from the name at the end of a link - the container from the extension, a
    packed alpha channel from an _alpha in the name - so the name carries them and the id is just
    the digits it starts with.
    """
    name = route.rsplit("/", 1)[1]
    digits = re.match(r"\d+", name)
    if not digits:
        raise ValueError(f"no video id in {name}")
    return int(digits.group())


class _Handler(BaseHTTPRequestHandler):
    server_version = "BlueShelf"
    protocol_version = "HTTP/1.1"
    insecure = False  # set on the plain-http handler below

    # ------------------------------------------------------------ plumbing

    @property
    def db(self) -> Database:
        return self.server.database_for_thread()

    def log_message(self, fmt, *args):  # quiet: this runs behind a desktop app
        pass

    def _send(self, status: HTTPStatus, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _authorised(self, query: dict) -> bool:
        token = self.server.token
        if not token:
            return True
        given = (query.get("k") or [""])[0]
        if given == token:
            return True
        cookie = self.headers.get("Cookie") or ""
        return f"blueshelf_key={token}" in cookie

    # ------------------------------------------------------------ routing

    def do_GET(self) -> None:
        self._route()

    def do_HEAD(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = parse_qs(parsed.query)
        # The passcode can be typed as part of the address - http://pc:8760/word - which is far
        # easier to enter in a headset than a query string. The word stays in the address and
        # every request the page makes goes back through it, so nothing depends on a cookie.
        prefix = f"/{self.server.token}" if self.server.token else ""
        by_path = bool(prefix) and (route == prefix or route.startswith(prefix + "/"))
        if by_path:
            if route == prefix:
                # Add the trailing slash, so "style.css" and the rest resolve under the word
                # rather than at the bare root, where they would be turned away.
                self._send(HTTPStatus.FOUND, b"", "text/plain; charset=utf-8",
                           {"Location": prefix + "/"})
                return
            route = route[len(prefix):]
            self._by_path = True
        authorised = by_path or self._authorised(query)
        if route == "/deovr" or route.startswith("/deovr/"):
            # DeoVR expects an answer either way: an empty library with "authorized": "0" is its
            # cue to ask for a password, rather than an error it cannot explain.
            self._deovr(route, authorised)
            return
        if not authorised:
            self._unauthorised(route)
            return
        try:
            if self.command == "POST":
                self._post(route)
            elif route.startswith("/api/"):
                self._api(route, query)
            elif route.startswith("/video/"):
                # The feed hands DeoVR its own links, so its plays can be told apart from the page's.
                self._stream(_video_id(route), (query.get("via") or ["page"])[0])
            elif route.startswith("/mask/"):
                self._mask(_video_id(route), (query.get("m") or [""])[0])
            elif route.startswith("/convert/"):
                self._transcode(_video_id(route), query)
            elif route.startswith("/thumb/"):
                self._thumb(int(route.rsplit("/", 1)[1].split(".")[0]))
            elif route.startswith("/strip/"):
                self._strip(int(route.rsplit("/", 1)[1].split(".")[0]))
            else:
                self._static(route, query)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the headset moved on to another video
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _base(self) -> str:
        """The address to build links from: however the client reached us, plus the word."""
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
        scheme = "http" if self.insecure else "https"
        prefix = f"/{self.server.token}" if self.server.token else ""
        return f"{scheme}://{host}{prefix}"

    def _deovr(self, route: str, authorised: bool) -> None:
        """DeoVR's own library format - its way in, and the one with a password."""
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
        if not authorised and body:
            try:
                given = json.loads(body).get("password") or ""
            except (ValueError, AttributeError):
                given = ""
            if self.server.token and given == self.server.token:
                authorised = True
            else:
                self._json(deovr.locked("-1"))
                return
        if not authorised:
            self._json(deovr.locked("0"))
            return
        # Always http, however DeoVR arrived: it refuses a certificate this PC signed for itself,
        # so an https link here means a scene that will not play.
        base = self._base().replace("https://", "http://", 1)
        if route.startswith("/deovr/video/"):
            wanted = route.rsplit("/", 1)[1]
            entry = deovr.single(self.db, int(wanted), base) if wanted.isdigit() else None
            if entry is None:
                self._json({"error": "No such video"}, HTTPStatus.NOT_FOUND)
                return
            self._json(entry)
            return
        layout = self.db.get_setting("deovr_layout", "grouped")
        self._json(deovr.feed(self.db, base, layout=layout if layout in deovr.LAYOUTS else "grouped"))

    def _unauthorised(self, route: str) -> None:
        """A person who typed the address without the word gets an explanation, not raw JSON."""
        if route.startswith(("/api/", "/video/", "/mask/", "/convert/", "/thumb/", "/strip/")):
            self._json({"error": "Wrong or missing passcode"}, HTTPStatus.UNAUTHORIZED)
            return
        host = self.headers.get("Host") or "this-pc"
        page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BlueShelf</title>
<style>body{{margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;
background:#141519;color:#e8e8ec;font:18px/1.5 system-ui,"Segoe UI",sans-serif;text-align:center}}
div{{max-width:30em;padding:24px}}code{{color:#f2545b}}</style></head>
<body><div><h1>BlueShelf</h1>
<p>This address needs your word on the end, like <code>http://{host}/yourword</code>.</p>
<p>On the PC, open BlueShelf, choose <b>Share with a headset, phone or TV</b>, and use the address shown
there.</p></div></body></html>"""
        self._send(HTTPStatus.UNAUTHORIZED, page.encode("utf-8"), "text/html; charset=utf-8")

    # ------------------------------------------------------------ static files

    def _static(self, route: str, query: dict) -> None:
        if self.insecure:
            # The page itself needs https, or the browser will not offer its VR mode. Everything
            # else - the feed, the videos, the thumbnails - answers plainly, because players other
            # than a browser will not accept a certificate this PC signed for itself.
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
            self._send(HTTPStatus.MOVED_PERMANENTLY, b"", "text/plain; charset=utf-8",
                       {"Location": f"https://{host}{self.path}"})
            return
        name = "index.html" if route in ("/", "") else route.lstrip("/")
        path = (WEB_DIR / name).resolve()
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"Not found")
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        extra = {}
        if name == "index.html" and self.server.token:
            extra["Set-Cookie"] = f"blueshelf_key={self.server.token}; Path=/; Max-Age=31536000; SameSite=Lax"
        self._send(HTTPStatus.OK, body, content_type, extra)

    # ------------------------------------------------------------ api

    def _video_json(self, row: dict, alone: bool = False) -> dict:
        """One video for the page. ``alone`` adds what is too slow to ask for every video.

        Looking beside a file for a mask means asking the disk, and a library listing runs
        this for every video it returns - over a network that is thousands of round trips
        for an answer only the video being opened needs.
        """
        return {
            "id": row["id"],
            "title": row["title"] or os.path.splitext(os.path.basename(row["path"]))[0],
            "performers": [p.strip() for p in (row.get("performer_names") or "").split(",") if p.strip()],
            "studio": row["studio"],
            "tags": [t.strip() for t in (row.get("tag_names") or "").split(",") if t.strip()],
            "duration": row["duration"] or 0,
            "width": row["width"] or 0,
            "height": row["height"] or 0,
            "size": row["size"],
            "rating": row["rating"],
            "favorite": bool(row["favorite"]),
            "resume": row["resume_pos"] or 0,
            "is_vr": bool(row["is_vr"]),
            "vr_layout": row["vr_layout"],
            "vr_proj": row["vr_proj"],
            "vr_lens_fov": row["vr_lens_fov"],
            # A cut-out kept beside the film instead of written into it. The player lays the
            # two together as it draws, so the film itself is never re-encoded.
            "masks": [name for name, _path in masks_beside(row["path"])] if alone else [],
            "has_mask": bool(mask_beside(row["path"])) if alone else False,
            # Marks along the scan bar, for a film that has chapters inside it.
            "chapters": media.chapters(row["path"]) if alone else [],
            # Stretches marked while watching, to be made into passthrough copies on the PC.
            "scenes": self.db.scenes(row["id"]) if alone else [],
            "codec": (row["vcodec"] or "").lower(),
            "container": os.path.splitext(row["path"])[1].lower().lstrip("."),
            "thumb": f"/thumb/{row['id']}.jpg" if row["thumb"] else "",
            "video": f"/video/{row['id']}",
            "convert": f"/convert/{row['id']}",
        }

    def _api(self, route: str, query: dict) -> None:
        db = self.db
        if route == "/api/status":
            count, size, _ = db.totals(False)
            self._json({"name": "BlueShelf", "videos": count, "size": size})
        elif route == "/api/performers":
            self._json([{"id": r["id"], "name": r["name"], "count": r["n"], "favorite": bool(r["favorite"])}
                        for r in db.performer_counts(False)])
        elif route == "/api/tags":
            self._json([{"id": r["id"], "name": r["name"], "count": r["n"]} for r in db.tag_counts(False)])
        elif route == "/api/library":
            performer = (query.get("performer") or [""])[0]
            tag = (query.get("tag") or [""])[0]
            view = (query.get("view") or ["all"])[0]
            rows = db.list_videos(search=(query.get("search") or [""])[0], view=view,
                                  performer_id=int(performer) if performer.isdigit() else None,
                                  tag_id=int(tag) if tag.isdigit() else None,
                                  sort=(query.get("sort") or ["added"])[0])
            self._json([self._video_json(row) for row in rows])
        elif route.startswith("/api/video/"):
            video = db.get_video(int(route.rsplit("/", 1)[1]))
            if video is None:
                self._json({"error": "No such video"}, HTTPStatus.NOT_FOUND)
                return
            video["performer_names"] = ", ".join(video["performers"])
            video["tag_names"] = ", ".join(video["tags"])
            self._json(self._video_json(video, alone=True))
        elif route.startswith("/api/strip/"):
            # The stills for the pointing bubble. Asking is what starts them being made, so a
            # film nobody opens never costs anything; the page asks again until they are ready.
            video = db.get_video(int(route.rsplit("/", 1)[1]))
            if video is None or not os.path.isfile(video["path"]):
                self._json({"ready": False, "failed": True})
                return
            self._json(media.STRIPS.status(video))
        else:
            self._json({"error": "Unknown request"}, HTTPStatus.NOT_FOUND)

    def _post(self, route: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        parts = route.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "video":
            self._json({"error": "Unknown request"}, HTTPStatus.NOT_FOUND)
            return
        video_id, action = int(parts[2]), parts[3]
        db = self.db
        video = db.get_video(video_id)
        if video is None:
            self._json({"error": "No such video"}, HTTPStatus.NOT_FOUND)
            return
        if action == "delete":
            self._json(self._delete(db, video))
        elif action == "favorite":
            db.set_fields(video_id, favorite=int(bool(body.get("value"))))
            self._json({"ok": True, "favorite": bool(body.get("value"))})
        elif action == "rating":
            db.set_fields(video_id, rating=max(0, min(5, int(body.get("value") or 0))))
            self._json({"ok": True, "rating": int(body.get("value") or 0)})
        elif action == "resume":
            db.save_resume(video_id, float(body.get("value") or 0))
            self._json({"ok": True})
        elif action == "lens":
            # The lens is guessed from the file's name and shape, and the guess is sometimes
            # wrong - a 200 degree fisheye read as 180 is warped in a way you only notice
            # once it is on your face. Setting it here locks it so nothing guesses again.
            proj = max(0, min(2, int(body.get("proj") or 0)))
            fov = max(100.0, min(360.0, float(body.get("fov") or 190)))
            layout = max(0, min(2, int(body.get("layout", video["vr_layout"]) or 0)))
            # "Flat": an ordinary film that was taken for a VR one. Its stereo layout still
            # counts - a 3D film for a flat screen is side by side too.
            is_vr = 0 if body.get("flat") else 1
            db.set_fields(video_id, is_vr=is_vr, vr_layout=layout, vr_proj=proj,
                          vr_lens_fov=fov, vr_locked=1)
            media.forget_strip(video_id)     # its stills were cut to the old shape
            self._json({"ok": True, "is_vr": bool(is_vr), "vr_proj": proj, "vr_lens_fov": fov,
                        "vr_layout": layout})
        elif action in ("scene-add", "scene-change", "scene-delete"):
            self._json(self._scene(db, video_id, action, body))
        else:
            self._json({"error": "Unknown action"}, HTTPStatus.NOT_FOUND)

    def _scene(self, db: Database, video_id: int, action: str, body: dict) -> dict:
        """Mark, move, rename or forget a scene. Always answers with the film's scenes as they
        now are, so the player never has to guess what the list looks like."""
        try:
            if action == "scene-add":
                db.add_scene(video_id, float(body.get("start")), float(body.get("end")), str(body.get("label") or ""))
            else:
                scene_id = int(body.get("id"))
                if db.scene_video(scene_id) != video_id:     # only this film's scenes, never another's
                    raise ValueError("no such scene in this film")
                if action == "scene-delete":
                    db.delete_scene(scene_id)
                else:
                    number = lambda key: None if body.get(key) is None else float(body[key])  # noqa: E731
                    label = body.get("label")
                    db.update_scene(scene_id, start=number("start"), end=number("end"),
                                    label=None if label is None else str(label))
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "scenes": db.scenes(video_id)}
        return {"ok": True, "scenes": db.scenes(video_id)}

    def _delete(self, db: Database, video: dict) -> dict:
        root = next((lib["path"] for lib in db.libraries() if lib["id"] == video["library_id"]),
                    os.path.dirname(video["path"]))
        permanent = db.get_setting("delete_mode", "trash") == "permanent"
        try:
            if permanent:
                trash.delete_permanently(video["path"], root)
                db.delete_videos([video["id"]])
            else:
                item, origin = trash.move_to_trash(video["path"], root)
                db.mark_trashed([video["id"]], item, origin)
        except OSError as exc:
            return {"ok": False, "error": exc.strerror or str(exc)}
        self.server.changed.set()
        return {"ok": True, "permanent": permanent, "title": video["title"]}

    # ------------------------------------------------------------ media

    def _thumb(self, video_id: int) -> None:
        path = paths.thumbs_dir() / f"{video_id}.jpg"
        if not path.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"No thumbnail")
            return
        self._send(HTTPStatus.OK, path.read_bytes(), "image/jpeg", {"Cache-Control": "max-age=86400"})

    def _strip(self, video_id: int) -> None:
        path = media.strip_path(video_id)
        if not path.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"Not made yet")
            return
        self._send(HTTPStatus.OK, path.read_bytes(), "image/jpeg", {"Cache-Control": "no-cache"})

    def _transcode(self, video_id: int, query: dict) -> None:
        """Convert to H.264 as it plays, for devices that cannot decode the original.

        The result is a live stream, so it has no length and cannot be seeked with byte ranges:
        the page asks again with a new start time instead.
        """
        video = self.db.get_video(video_id)
        if video is None or not os.path.isfile(video["path"]):
            self._send(HTTPStatus.NOT_FOUND, b"File not found")
            return
        quality = QUALITIES.get((query.get("q") or ["high"])[0], QUALITIES["high"])
        try:
            start = max(0.0, float((query.get("seek") or ["0"])[0]))
        except ValueError:
            start = 0.0
        encoder = video_encoder()
        width = min(quality["width"], video["width"] or quality["width"])
        width -= width % 2
        if "h264_nvenc" in encoder:
            # Decode, scale and encode all on the graphics card: the frames never come back to the
            # CPU, which is what makes 6K playable in real time.
            decode = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            scale = ["-vf", f"scale_cuda={width}:-2"]
            pixels = []
        else:
            decode = ["-hwaccel", "auto"]
            scale = ["-vf", f"scale={width}:-2"]
            pixels = ["-pix_fmt", "yuv420p"]
        command = [paths.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", *decode]
        if start:
            command += ["-ss", f"{start:.2f}"]
        command += [
            "-i", video["path"],
            "-map", "0:v:0", "-map", "0:a:0?",
            *scale,
            *encoder,
            "-b:v", quality["bitrate"], "-maxrate", quality["bitrate"], "-bufsize", "24M",
            "-g", "60", *pixels,
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
        ]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, creationflags=paths.NO_WINDOW)
        viewer = self.client_address[0]
        take_over_conversion(viewer, process)
        self.connection.settimeout(STALLED_VIEWER)
        sent = 0
        try:
            while True:
                data = process.stdout.read(CHUNK)
                if not data:
                    break          # finished - or stopped because this viewer asked for another
                self.wfile.write(data)
                sent += len(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass  # the viewer skipped on, closed the page, or stopped taking anything
        finally:
            let_go_of_conversion(viewer, process)
            process.kill()
            process.stdout.close()
            if not sent:  # nothing played: keep the reason rather than failing silently
                try:
                    message = process.stderr.read(2000).decode("utf-8", "replace").strip()
                    with open(paths.data_dir() / "blueshelf.log", "a", encoding="utf-8") as log:
                        log.write(f"\nConverting {video['path']} produced nothing:\n{message}\n")
                except Exception:
                    pass
            process.stderr.close()
            process.wait(timeout=10)

    def _stream(self, video_id: int, source: str = "page") -> None:
        video = self.db.get_video(video_id)
        if video is None or not os.path.isfile(video["path"]):
            self._send(HTTPStatus.NOT_FOUND, b"File not found")
            return
        # Only the first request for a file counts: a seek is not a second play.
        start_of_file = not (self.headers.get("Range") or "").startswith("bytes=") or \
            (self.headers.get("Range") or "").startswith("bytes=0-")
        if start_of_file:
            count_play(self.db, video_id, self.client_address[0], source)
        self._send_range(video["path"])

    def _mask(self, video_id: int, name: str = "") -> None:
        """A cut-out on its own, for a player that lays it over the film as it draws.

        ``name`` picks one when a film has several - from different models, say. Only names
        from the list found beside the film are served, so this can never be pointed at an
        arbitrary file. Not counted as a play: the film it belongs to is being counted at the
        same moment, and one viewing should not appear as two.
        """
        video = self.db.get_video(video_id)
        path = mask_beside(video["path"], name) if video else ""
        if not path:
            self._send(HTTPStatus.NOT_FOUND, b"No mask for that video")
            return
        self._send_range(path)

    def _send_range(self, path: str) -> None:
        """A file, honouring a Range request. Players seek constantly, so this is not optional."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        requested = self.headers.get("Range")
        match = _RANGE.match(requested or "")
        if match:
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            elif match.group(2):  # bytes=-500 means the last 500 bytes
                start = max(0, size - int(match.group(2)))
        if start >= size:
            self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", extra={"Content-Range": f"bytes */{size}"})
            return
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if match else HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if match:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(CHUNK, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)


class _PlainHandler(_Handler):
    """The same server, for connections that arrived without TLS."""

    insecure = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token: str, ssl_context: ssl.SSLContext | None = None):
        super().__init__(address, _Handler)
        self.token = token
        self.ssl_context = ssl_context
        self.changed = threading.Event()
        self._local = threading.local()

    def finish_request(self, request, client_address) -> None:
        """Works out whether this connection is https or plain http before handling it."""
        if self.ssl_context is not None:
            try:
                request.settimeout(15)
                first = request.recv(1, socket.MSG_PEEK)
                request.settimeout(None)
            except OSError:
                return
            if first != b"":  # not the start of a TLS handshake, so it is plain http
                _PlainHandler(request, client_address, self)
                return
            try:
                request = self.ssl_context.wrap_socket(request, server_side=True)
            except OSError:
                return  # a browser refusing the certificate, or a port scanner
        self.RequestHandlerClass(request, client_address, self)

    def database_for_thread(self) -> Database:
        if getattr(self._local, "db", None) is None:
            self._local.db = Database(str(paths.db_path()))
        return self._local.db


class RemoteServer:
    """Starts and stops the web server. Safe to call start() twice."""

    def __init__(self, token: str = "", port: int = DEFAULT_PORT, secure: bool = True):
        self.token = token
        self.port = port
        self.secure = secure  # https, so the headset browser offers its VR mode
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def scheme(self) -> str:
        return "https" if (self._server is not None and self._server.ssl_context) else "http"

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def changed(self) -> bool:
        """True when something was deleted or edited from a headset since this was last asked."""
        if self._server is not None and self._server.changed.is_set():
            self._server.changed.clear()
            return True
        return False

    def start(self) -> int:
        if self._server is not None:
            return self.port
        self._server = _Server(("0.0.0.0", self.port), self.token, self._ssl_context())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="blueshelf-remote", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server, self._thread = None, None

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.secure:
            return None
        pair = certs.ensure()
        if pair is None:
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(certfile=pair[0], keyfile=pair[1])
        except (ssl.SSLError, OSError):
            return None
        return context

    def addresses(self) -> list[str]:
        return local_addresses(self.port, self.scheme)
