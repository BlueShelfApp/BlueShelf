"""What the player shows about a film besides the film: its chapters, and a strip of stills
for the pointing bubble over the scan bar.

Asked for when one film is opened, never for a whole library listing - each answer means
opening the file, and over a network a listing would be thousands of those.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from blueshelf import paths

_chapters: dict[str, tuple[float, int, list]] = {}
_chapters_lock = threading.Lock()


def chapters(path: str) -> list[dict]:
    """The chapters marked inside the file, if it has any. Remembered until the file changes,
    so opening the same film again costs nothing."""
    try:
        stat = os.stat(path)
    except OSError:
        return []
    with _chapters_lock:
        known = _chapters.get(path)
        if known and known[0] == stat.st_mtime and known[1] == stat.st_size:
            return known[2]
    found: list[dict] = []
    try:
        said = paths.run_tool([paths.tool("ffprobe"), "-v", "error", "-print_format", "json",
                               "-show_chapters", path], timeout=60)
        data = json.loads((said.stdout or b"{}").decode("utf-8", "replace") or "{}")
    except Exception:
        data = {}
    for chapter in data.get("chapters") or []:
        try:
            start = float(chapter.get("start_time") or 0)
        except (TypeError, ValueError):
            continue
        title = ((chapter.get("tags") or {}).get("title") or "").strip()
        found.append({"start": start, "title": title or f"Chapter {len(found) + 1}"})
    with _chapters_lock:
        _chapters[path] = (stat.st_mtime, stat.st_size, found)
    return found


# --------------------------------------------------------------------------- the strip of stills

STRIP_COLS, STRIP_ROWS = 10, 10
STRIP_COUNT = STRIP_COLS * STRIP_ROWS
TILE_W, TILE_H = 160, 90
# Below normal, so a strip being made never takes the processor from playback or matting.
LOW_PRIORITY = 0x00004000 if os.name == "nt" else 0


def strip_path(video_id: int) -> Path:
    return paths.data_dir() / "strips" / f"{video_id}.jpg"


def strip_info(video_id: int) -> dict:
    return {"ready": True, "url": f"/strip/{video_id}.jpg", "count": STRIP_COUNT, "cols": STRIP_COLS,
            "tileW": TILE_W, "tileH": TILE_H}


def forget_strip(video_id: int) -> None:
    """Made with the old lens, so it would show the wrong picture - it is made again."""
    try:
        strip_path(video_id).unlink()
    except OSError:
        pass


def tile_filter(video: dict) -> str:
    if video.get("is_vr"):
        from blueshelf.scanner import vr_flat_filter
        return vr_flat_filter(video.get("vr_layout") or 0, video.get("vr_proj") or 0,
                              video.get("vr_lens_fov") or 190, TILE_W, TILE_H)
    return (f"scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,"
            f"pad={TILE_W}:{TILE_H}:(ow-iw)/2:(oh-ih)/2")


# Stills taken at once. Each is mostly waiting on the network and one decoder thread, so three
# together are nearly three times as fast without crowding out playback or matting.
AT_ONCE = 3


def _still(video: dict, at: float, vf: str, into: Path) -> bool:
    try:
        subprocess.run([paths.tool("ffmpeg"), "-v", "error", "-y", "-skip_frame", "nokey",
                        "-ss", f"{at:.2f}", "-i", video["path"], "-frames:v", "1", "-update", "1",
                        "-vf", vf, "-q:v", "5", str(into)],
                       capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                       creationflags=paths.NO_WINDOW | LOW_PRIORITY)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return into.is_file() and into.stat().st_size > 0


def make_strip(video: dict, into: Path, along=lambda _done: None) -> bool:
    """A hundred stills across the film, tiled ten by ten into one picture.

    One seek per still, decoding only the next key frame after it: a still a second or two
    late is invisible at this size, and it means reading a few megabytes per still rather
    than the whole film - which over a network is the difference between a minute and ten.
    Past the last key frame there is nothing to find, so a still that comes out empty takes
    its neighbour's. ``along`` is told how many are done, for the page to show.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    duration = float(video.get("duration") or 0)
    if duration <= 0:
        return False
    work = Path(tempfile.mkdtemp(prefix="blueshelf-strip-"))
    try:
        vf = tile_filter(video)
        stills = [work / f"{i:03d}.jpg" for i in range(STRIP_COUNT)]
        made = 0
        with ThreadPoolExecutor(AT_ONCE) as pool:
            jobs = [pool.submit(_still, video, (i + 0.5) * duration / STRIP_COUNT, vf, stills[i])
                    for i in range(STRIP_COUNT)]
            for finished in as_completed(jobs):
                made += bool(finished.result())
                along(made)
        if made < STRIP_COUNT // 2:
            return False
        # Gaps take the nearest still before them, or after them at the very start.
        have = [i for i, still in enumerate(stills) if still.is_file() and still.stat().st_size]
        for i, still in enumerate(stills):
            if i not in have:
                nearest = max((j for j in have if j < i), default=min(have))
                shutil.copyfile(stills[nearest], still)
        into.parent.mkdir(parents=True, exist_ok=True)
        partial = into.with_suffix(".part.jpg")
        subprocess.run([paths.tool("ffmpeg"), "-v", "error", "-y", "-framerate", "1",
                        "-i", str(work / "%03d.jpg"), "-vf", f"tile={STRIP_COLS}x{STRIP_ROWS}",
                        "-frames:v", "1", "-q:v", "4", str(partial)],
                       capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                       creationflags=paths.NO_WINDOW | LOW_PRIORITY)
        if not partial.is_file():
            return False
        os.replace(partial, into)
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


class StripMaker:
    """Makes strips one film at a time, in the background, for the films people actually open."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: list[dict] = []
        self._busy: set[int] = set()
        self._failed: set[int] = set()
        self._done: dict[int, int] = {}
        self._thread: threading.Thread | None = None

    def status(self, video: dict) -> dict:
        video_id = video["id"]
        if strip_path(video_id).is_file():
            return strip_info(video_id)
        with self._lock:
            if video_id in self._failed:
                return {"ready": False, "failed": True}
            if video_id not in self._busy:
                self._busy.add(video_id)
                self._done[video_id] = 0
                self._queue.append(dict(video))
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._work, daemon=True)
                    self._thread.start()
            return {"ready": False, "making": True, "done": self._done.get(video_id, 0), "count": STRIP_COUNT}

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._thread = None
                    return
                video = self._queue.pop()      # the film opened most recently first
            video_id = video["id"]

            def along(done: int, video_id=video_id) -> None:
                self._done[video_id] = done

            try:
                done = make_strip(video, strip_path(video_id), along)
            except Exception:
                done = False
            with self._lock:
                self._busy.discard(video_id)
                self._done.pop(video_id, None)
                if not done:
                    self._failed.add(video_id)


STRIPS = StripMaker()
