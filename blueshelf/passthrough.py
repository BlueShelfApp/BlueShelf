"""Making a passthrough copy of a video, by driving VRAutoMatte from inside BlueShelf.

A passthrough video is an ordinary VR video with a cut-out of the performer packed into the
corners of each frame, in the layout DeoVR reads. Studios that sell them shoot against a green
screen; this makes one from a video that never had it, by asking a matting model which pixels
are the person.

BlueShelf does not carry that model. VRAutoMatte does, and it brings PyTorch and a CUDA build
with it - several gigabytes, and no use at all without an NVIDIA card. Bundling that into a
free download most of whose users could not run it would be a poor trade, so BlueShelf finds a
VRAutoMatte install instead and runs blueshelf/matte_worker.py with *its* interpreter. See
farm.py for spreading one video across several machines.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator

from blueshelf import paths

# Where to look when the setting is empty. Most people clone it next to whatever else they
# keep, so the folders around BlueShelf are worth a glance before giving up and asking.
GUESSES = (
    Path("vrautomatte"),
    Path("VRAutoMatte"),
    Path("..") / "vrautomatte",
    Path("..") / "VRAutoMatte",
)

Progress = Callable[[str, float, str], None]  # stage, 0..1, a line of detail


class Unavailable(RuntimeError):
    """No usable VRAutoMatte - the message says what is missing and what to do about it."""


# --------------------------------------------------------------------------- finding it

def python_of(root: str | Path) -> Path:
    """The interpreter inside a VRAutoMatte checkout's virtual environment."""
    root = Path(root)
    name = "python.exe" if os.name == "nt" else "python"
    return root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / name


def has_matanyone2(root: str | Path) -> bool:
    """Whether this install can do the telling-it-what-to-track kind of matting.

    MatAnyone2 and SAM2 are optional extras, and the published MatAnyone2 will not build
    without a fix, so most installs have neither. Looked for on disk rather than by asking
    the interpreter, which would cost a second and load torch.
    """
    for where in ("Lib", "lib"):
        if (Path(root) / ".venv" / where / "site-packages" / "matanyone2").is_dir():
            return True
    return False


def looks_like_install(root: str | Path) -> bool:
    root = Path(root)
    return (root / "src" / "vrautomatte" / "main.py").is_file() and python_of(root).is_file()


def find_install(saved: str | None = None) -> Path | None:
    """The install to use: the remembered one if it is still there, else a look around."""
    if saved and looks_like_install(saved):
        return Path(saved)
    here = Path(__file__).resolve().parent.parent
    for base in (here, here.parent, Path.cwd()):
        for guess in GUESSES:
            candidate = (base / guess).resolve()
            if looks_like_install(candidate):
                return candidate
    return None


# --------------------------------------------------------------------------- running it

def worker_script() -> Path:
    """matte_worker.py, wherever this copy of BlueShelf keeps it.

    In a built copy the package lives inside the bundle, so the file is read out to a real
    path VRAutoMatte's interpreter can open.
    """
    packaged = Path(__file__).resolve().parent / "matte_worker.py"
    if packaged.is_file():
        return packaged
    loose = paths.PACKAGE_DIR / "matte_worker.py"
    if loose.is_file():
        return loose
    raise Unavailable("BlueShelf is missing matte_worker.py - the install looks incomplete.")


def _environment() -> dict:
    """VRAutoMatte calls ffmpeg by name, so BlueShelf's own copy goes on the PATH for it."""
    env = dict(os.environ)
    if paths.BIN_DIR.exists():
        env["PATH"] = str(paths.BIN_DIR) + os.pathsep + env.get("PATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Its own launcher sets this; without it the loguru colour codes end up in our output.
    env["NO_COLOR"] = "1"
    return env


class Run:
    """One worker process, watched line by line.

    The worker prints a JSON object per line. Anything that is not JSON is log noise, kept
    only so a failure can be explained afterwards.
    """

    def __init__(self, root: str | Path, spec: dict):
        self.root = Path(root)
        self.spec = spec
        self.process: subprocess.Popen | None = None
        self.noise: list[str] = []
        self.result: dict | None = None
        self.error: str | None = None
        self._spec_file: str | None = None

    def start(self) -> None:
        python = python_of(self.root)
        if not python.is_file():
            raise Unavailable(
                f"VRAutoMatte has no virtual environment at {python}. Install uv "
                f"(winget install astral-sh.uv) and run 'uv sync' in that folder to build "
                f"one, then try again.")
        handle, self._spec_file = tempfile.mkstemp(suffix=".json", prefix="blueshelf-matte-")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(self.spec, out)
        self.process = subprocess.Popen(
            [str(python), str(worker_script()), self._spec_file],
            cwd=str(self.root), env=_environment(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=paths.NO_WINDOW)

    def lines(self) -> Iterator[dict]:
        """Every message the worker sends, in order, until it stops."""
        if self.process is None:
            self.start()
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    message = json.loads(line)
                except ValueError:
                    self.noise.append(line)
                    continue
                if message.get("event") == "done":
                    self.result = message
                elif message.get("event") == "error":
                    self.error = message.get("message", "it stopped without saying why")
                    self.noise.append(message.get("detail", ""))
                yield message
            else:
                self.noise.append(line)
                del self.noise[:-400]
        self.process.wait()
        if self.result is None and self.error is None:
            tail = "\n".join(n for n in self.noise[-12:] if n)
            self.error = f"VRAutoMatte stopped early (exit {self.process.returncode}).\n{tail}"

    def stop(self) -> None:
        """Stop the worker, and everything it started.

        Killing the worker alone is not enough. It spends nearly all its time waiting on an
        ffmpeg or ffprobe of its own, and those are not its children to Windows once it dies -
        they carry on reading the file, holding it open, for as long as their timeout allows.
        One was found still running long after the program that asked for it had gone.
        """
        if not self.process or self.process.poll() is not None:
            return
        if os.name == "nt":
            # taskkill walks the tree; terminate() does not.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                           capture_output=True, creationflags=paths.NO_WINDOW)
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def cleanup(self) -> None:
        if self._spec_file:
            try:
                os.unlink(self._spec_file)
            except OSError:
                pass
            self._spec_file = None


def describe(root: str | Path, timeout: float = 240.0) -> dict:
    """Ask an install what it can do. Slow the first time: importing torch wakes the card."""
    run = Run(root, {"mode": "probe"})
    deadline = time.monotonic() + timeout
    try:
        for _message in run.lines():
            if time.monotonic() > deadline:
                run.stop()
                return {"ok": False, "error": "VRAutoMatte did not answer in time."}
    finally:
        run.cleanup()
    if run.result:
        return run.result
    return {"ok": False, "error": run.error or "no answer"}


def look(root: str | Path, source: str | Path, report: Progress,
         should_stop: Callable[[], bool] = lambda: False) -> dict:
    """Read a video through cheaply and report where the action is."""
    run = Run(root, {"mode": "look", "source": str(Path(source).resolve())})
    try:
        for message in run.lines():
            if should_stop():
                run.stop()
                raise InterruptedError("Cancelled.")
            if message.get("event") == "progress":
                report(message.get("stage", ""), float(message.get("share", 0.0)), "")
    finally:
        run.cleanup()
    if run.error:
        raise Unavailable(run.error)
    return run.result


# --------------------------------------------------------------------------- the job itself

@dataclass
class Job:
    """What to make, and how. One of these describes a whole video, split or not."""

    source: str
    output: str = ""
    projection: str = "equirectangular"   # or "fisheye", when the file already is
    fov: int = 180
    model: str = "mobilenetv3"            # or "resnet50", slower and a little cleaner
    sbs: bool = True
    crf: int = 18
    stride: int = 1
    roi: bool = True
    smoothing: float = 1.0
    detail: str = "fine"                  # how much of the frame the model is shown
    pack_chunk: int = 0                   # frames per piece when projecting and packing
    frames: int = 0                       # how many the source has, filled in by inspect()
    fps: float = 0.0
    width: int = 0
    height: int = 0
    parts: int = 1                        # how many machines share it
    # The cut-out's own size. 0 keeps it the size of the film, as every job filed before
    # small masks was - so a job already in the queue finishes exactly as it started.
    mask_w: int = 0
    mask_h: int = 0
    make: str = "deovr"                   # "deovr" packs a copy; "matte" stops at the cut-out
    keep_mask: bool = False               # after packing, also keep the cut-out for BlueShelf
    extras: dict = field(default_factory=dict)

    @property
    def small_mask(self) -> bool:
        return self.mask_w > 0 and self.mask_h > 0

    def spec(self, **overrides) -> dict:
        out = {k: v for k, v in asdict(self).items()
               if k not in ("frames", "fps", "width", "height", "parts", "extras", "detail",
                            "pack_chunk", "mask_w", "mask_h", "make", "keep_mask")}
        budget, ratio = DETAIL.get(self.detail, DETAIL["fine"])
        out["max_matting_pixels"], out["downsample_ratio"] = budget, ratio
        out["small_mask"] = self.small_mask
        out.update(self.extras)
        out.update(overrides)
        return out


# How much of each eye the matting model is allowed to see, and how finely it works inside.
#
# VRAutoMatte sizes this from the card's memory, and its table is far too cautious: an 8 GB
# card is dropped to its second-coarsest setting, which shows the model about 960x960 of a
# 3584x3584 eye and then scales the matte back up almost fourfold. The result has soft edges
# and, measured on a real scene, green showing through the performer's body.
#
# The same card ran the 15 GB tier's settings without complaint, and 17% slower. So the choice
# is made here instead, and "fine" is the default.
DETAIL = {
    "coarse": (1280 * 720, 0.125),
    "normal": (1440 * 810, 0.125),
    "fine": (1920 * 1080, 0.25),
    "finest": (2560 * 1440, 0.25),
}
def mask_size_of(job: Job) -> tuple[int, int]:
    """The size this job's chunks and joined cut-out are: small, or the film's own."""
    return (job.mask_w, job.mask_h) if job.small_mask else (job.width, job.height)


def settle_mask_size(job: Job) -> None:
    """Decide the cut-out's size for this job: small, unless there is older work to match.

    Chunks already matted at the film's full size by an earlier run cannot be joined to new
    small ones, so a film with work on disk keeps whatever size that work was made at -
    recorded beside it, or full size if it was made before the size was recorded at all.
    """
    folder = work_folder(job)
    try:
        started = folder.is_dir() and any(folder.glob("part-*.mp4"))
    except OSError:
        started = False
    if started:
        saved = read_settings(job) or {}
        job.mask_w, job.mask_h = int(saved.get("mask_w", 0)), int(saved.get("mask_h", 0))
    else:
        job.mask_w, job.mask_h = working_mask_size(job.width, job.height, job.detail, job.sbs)


def working_mask_size(width: int, height: int, detail: str,
                      sbs: bool = True) -> tuple[int, int]:
    """The size the matting model actually works at - which is the size worth keeping the
    cut-out at, and no bigger.

    VRAutoMatte shrinks the frame to its pixel budget, per eye when the film is side by side,
    mattes it there, then blew the result up to the film's full size. For a 7K film at
    "fine" that is 2880x1440 enlarged to 7168x3584: every chunk written at 7K, joined at 7K,
    and then shrunk again by the packer, which only wants it at 40%. Keeping it at this size
    loses nothing the model produced. Worked out the same way VRAutoMatte does it, so its
    chunks arrive at exactly this size.
    """
    if width <= 0 or height <= 0:
        return 0, 0
    budget, _ratio = DETAIL.get(detail, DETAIL["fine"])
    side_by_side = sbs and width / height >= 1.9
    eye_w = width // 2 if side_by_side else width
    if budget > 0 and eye_w * height > budget:
        scale = (budget / (eye_w * height)) ** 0.5
        eye_w, h = int(eye_w * scale) & ~1, int(height * scale) & ~1
    else:
        h = height
    return (eye_w * 2 if side_by_side else eye_w), h


DETAIL_WHY = {
    "coarse": "fastest, and soft at the edges - for a card with little memory",
    "normal": "a little sharper",
    "fine": "sharp edges, and about a sixth slower - what most cards should use",
    "finest": "sharper still, and slower again - needs a roomy card",
}


LENS = re.compile(r"_(FISHEYE|MKX|VRCA|RF52|EQUIRECT)[0-9]*", re.I)
ALREADY = re.compile(r"_alpha(\.[a-z0-9]+)?$", re.I)


def output_name(source: str | Path, fov: int = 180) -> str:
    """The name DeoVR and BlueShelf both read: the lens and FISHEYE<fov>_alpha on the end.

    A file that already carries a lens tag keeps only one - two would leave the player
    guessing.
    """
    source = Path(source)
    stem = LENS.sub("", source.stem)
    stem = ALREADY.sub("", stem)
    return f"{stem}_FISHEYE{fov}_alpha.mp4"


ANGLE = re.compile(
    r"(?:FISHEYE|FISH|MKX|VRCA|RF)[ _-]?(\d{3})"      # SLR and friends: FISHEYE190
    r"|(?:LR|TB|SBS|MONO)[ _-](\d{3})(?:x\d{3})?"    # the other habit: _LR_180
    r"|(?:^|[ _-])(\d{3})x(\d{3})(?:[ _-]|$)",        # and _180x180_
    re.I)


def lens_angle(name: str) -> int | None:
    """The lens angle a file name claims, if it claims one.

    Worth reading rather than assuming: a copy made from a 190 file and then named 180 would
    play back at the wrong width in the headset, with the edges of the scene pulled inward.
    """
    found = ANGLE.search(name)
    if not found:
        return None
    lens, paired, across, down = found.groups()
    if across and down:
        # A pair like 180x180 only says a lens angle when both halves agree.
        digits = across if across == down else None
    else:
        digits = lens or paired
    if not digits:
        return None
    angle = int(digits)
    return angle if 140 <= angle <= 220 else None


def guess_projection(width: int, height: int, name: str) -> str:
    """Fisheye files say so in the name; everything else in a 2:1 frame is equirectangular."""
    if re.search(r"fisheye|_mkx|vrca|_rf52|190|200|220", name, re.I):
        return "fisheye"
    return "equirectangular"


def measure(source: str | Path) -> dict:
    """Ask the file itself how big it is and how many frames it holds."""
    probe = paths.run_tool([
        paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-print_format", "json",
        "-show_streams", "-show_format", str(source)], timeout=180)
    try:
        stream = json.loads(probe.stdout or b"{}")["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise Unavailable(f"ffprobe could not read {Path(source).name}.")
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    top, _, bottom = rate.partition("/")
    fps = float(top) / float(bottom or 1) if float(bottom or 1) else 0.0
    counted = stream.get("nb_frames")
    if counted and counted.isdigit() and int(counted) > 0:
        frames = int(counted)
    else:
        duration = float(stream.get("duration") or 0) or _format_duration(probe.stdout)
        frames = int(round(duration * fps)) if fps else 0
    return {"width": int(stream.get("width", 0)), "height": int(stream.get("height", 0)),
            "fps": fps, "frames": frames}


def facts_from_library(row) -> dict | None:
    """The same numbers out of the library, when it already has them.

    Worth the trouble: picking a performer with fifty scenes would otherwise mean fifty
    ffprobe calls before the window could even draw the estimate.
    """
    try:
        width, height = int(row["width"] or 0), int(row["height"] or 0)
        fps, duration = float(row["fps"] or 0), float(row["duration"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not (width and height and fps > 0 and duration > 0):
        return None
    return {"width": width, "height": height, "fps": fps,
            "frames": int(round(duration * fps))}


def inspect(job: Job, facts: dict | None = None) -> Job:
    """Fill in what the file says: size, frame rate, frame count, projection, lens and name."""
    # Absolute from here on. The worker runs from VRAutoMatte's own folder, and on the farm
    # another machine reads this path entirely - a relative one means nothing to either.
    job.source = str(Path(job.source).resolve())
    found = facts or measure(job.source)
    job.width, job.height = found["width"], found["height"]
    job.fps, job.frames = found["fps"], found["frames"]
    if not job.extras.get("projection_locked"):
        job.projection = guess_projection(job.width, job.height, Path(job.source).name)
    if not job.extras.get("fov_locked"):
        job.fov = lens_angle(Path(job.source).name) or job.fov
    if not job.output:
        job.output = str(output_folder(job.source) / output_name(job.source, job.fov))
    return job


def _format_duration(raw: bytes) -> float:
    try:
        return float(json.loads(raw or b"{}").get("format", {}).get("duration") or 0)
    except ValueError:
        return 0.0


# Roughly what each setting costs against the middle one, measured on a real scene.
DETAIL_COST = {"coarse": 0.85, "normal": 1.0, "fine": 1.17, "finest": 1.45}


def estimate_minutes(job: Job, fps_per_eye: float = 25.0) -> float:
    """A rough figure for the waiting, so nobody starts an overnight job by accident.

    fps_per_eye is what one card manages on one eye; a side-by-side frame needs two passes.
    25 is about right for RVM mobilenet on a laptop RTX 2000 Ada at 8K, and the number is
    replaced by a measured one as soon as a job has run.
    """
    if not job.frames or fps_per_eye <= 0:
        return 0.0
    passes = 2 if job.sbs else 1
    matting = job.frames * passes / fps_per_eye / 60.0 * DETAIL_COST.get(job.detail, 1.0)
    return round((matting + job.frames / 3000.0) / max(1, job.parts), 1)


# --------------------------------------------------------------------------- splitting it up

@dataclass
class Slice:
    """One machine's share of a video.

    ``start`` is where it begins matting and ``keep_from`` where its output is actually used.
    The gap between them is the lead-in, and it is not optional: the matting model carries
    state from frame to frame, so one started cold in the middle of a scene gets the first
    second or two wrong. It mattes a little extra ahead of its share and that part is thrown
    away, which is the price of splitting a video that was meant to be watched in one pass.
    """

    index: int
    start: int          # 1-based, inclusive - where matting starts
    keep_from: int      # 1-based, inclusive - the first frame that counts
    end: int            # 1-based, inclusive
    blank: bool = False  # a stretch nobody needs matted: filled in with an opaque mask

    @property
    def lead_in(self) -> int:
        return self.keep_from - self.start

    @property
    def kept(self) -> int:
        return self.end - self.keep_from + 1


@dataclass
class Stretch:
    """A run of frames worth matting. 1-based, inclusive, like everything else here."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def suggest_threshold(values: list[float], lowest: float = 0.04,
                     highest: float = 0.45) -> float:
    """Where to put the line between quiet and action, taken from this video's own spread.

    A fixed number does not survive contact with VR. A POV scene keeps a performer close to
    the lens the whole time, so even the talking runs at a skin fraction that would be a
    climax in a flat scene - measured at 0.20 on a real file whose peaks reached 0.46.

    So the split is chosen the way a photograph is thresholded: find the value that best
    separates the samples into two groups (Otsu's method). It says nothing about whether the
    two groups are talking and action - only that the video has two groups and this is where
    they part. Which is why the answer is drawn on screen to be argued with.
    """
    usable = [v for v in values if v > 0]
    if len(usable) < 8:
        return 0.12
    steps = 64
    top = max(usable)
    if top <= 0:
        return 0.12
    counts = [0] * steps
    for value in usable:
        counts[min(steps - 1, int(value / top * steps))] += 1
    total = len(usable)
    best, best_at = -1.0, steps // 2
    behind = ahead = 0.0
    weight_behind = 0
    for index in range(steps):
        behind += index * counts[index]
    for index in range(steps):
        weight_behind += counts[index]
        weight_ahead = total - weight_behind
        if weight_behind == 0 or weight_ahead == 0:
            continue
        ahead += counts[index] * index
        mean_behind = ahead / weight_behind
        mean_ahead = (behind - ahead) / weight_ahead
        spread = weight_behind * weight_ahead * (mean_behind - mean_ahead) ** 2
        if spread > best:
            best, best_at = spread, index
    return max(lowest, min(highest, (best_at + 1) / steps * top))


def stretches_from(times: list[float], values: list[float], fps: float, frames: int,
                   threshold: float = 0.10, pad: float = 5.0, shortest: float = 20.0,
                   bridge: float = 45.0) -> list[Stretch]:
    """Turn a reading of how much skin is on screen into the stretches worth matting.

    Three corrections matter more than the threshold itself:

    * **Padding.** The reading is taken every few seconds, so a stretch really began somewhere
      before the first sample that noticed it. Each one is widened at both ends.
    * **Bridging.** A short lull should not split one scene into two, because every restart
      costs a fresh run-up and risks the cut-out blinking off for a moment.
    * **A floor on length.** A single stray sample is noise, not a scene.

    The bias throughout is towards matting too much. Matting a minute that did not need it
    wastes a minute; missing one means the action plays with no cut-out at all.
    """
    if not values or fps <= 0 or frames <= 0:
        return []

    def at(index: int) -> int:
        when = times[index] if index < len(times) else index * 1.0
        return max(1, min(frames, int(round(when * fps)) + 1))

    runs: list[list[int]] = []
    for index, value in enumerate(values):
        if value < threshold:
            continue
        here = at(index)
        # How far this sample speaks for: up to the next one.
        until = at(index + 1) - 1 if index + 1 < len(values) else frames
        until = max(here, until)
        if runs and here <= runs[-1][1] + 1:
            runs[-1][1] = max(runs[-1][1], until)
        else:
            runs.append([here, until])
    if not runs:
        return []

    pad_frames = int(round(pad * fps))
    widened = [[max(1, a - pad_frames), min(frames, b + pad_frames)] for a, b in runs]

    bridge_frames = int(round(bridge * fps))
    joined = [widened[0]]
    for a, b in widened[1:]:
        if a - joined[-1][1] <= bridge_frames:
            joined[-1][1] = max(joined[-1][1], b)
        else:
            joined.append([a, b])

    floor = int(round(shortest * fps))
    return [Stretch(a, b) for a, b in joined if b - a + 1 >= floor]


def pieces_for(length: int, parts: int, chunk: int) -> int:
    """How many pieces one run of frames should be cut into.

    A chunk size wins over a number of machines, because it is the stronger claim: it says
    how much work may be lost when something goes wrong, and that matters more than how the
    work is shared out. Several machines still help - there are simply more, smaller pieces
    for them to take.
    """
    if chunk > 0:
        return max(1, -(-length // chunk))
    return max(1, min(parts, length))


def plan_slices(frames: int, stretches: list[Stretch] | None, parts: int = 1,
                lead_in: int = 60, chunk: int = 0) -> list[Slice]:
    """The whole video as an ordered run of pieces: some to be matted, some left blank.

    Every frame belongs to exactly one piece, so the finished mask is the same length as the
    picture however the video was carved up.

    ``chunk`` caps how many frames one piece may cover. Matting a forty-minute scene in one
    go means that anything going wrong at the end - and something does - throws away the lot.
    In chunks, a failure costs one chunk, and the ones already done are still on disk to be
    picked up again.
    """
    if frames <= 0:
        raise ValueError("the video has no frames to divide")
    if not stretches:
        return split(frames, parts, lead_in, chunk)

    tidy: list[Stretch] = []
    for stretch in sorted(stretches, key=lambda s: s.start):
        a, b = max(1, stretch.start), min(frames, stretch.end)
        if b < a:
            continue
        if tidy and a <= tidy[-1].end + 1:
            tidy[-1] = Stretch(tidy[-1].start, max(tidy[-1].end, b))
        else:
            tidy.append(Stretch(a, b))
    if not tidy:
        return split(frames, parts, lead_in, chunk)

    out: list[Slice] = []
    at = 1
    for stretch in tidy:
        if stretch.start > at:
            out.append(Slice(0, at, at, stretch.start - 1, blank=True))
        share = pieces_for(stretch.length, parts, chunk)
        edge, here = stretch.length // share, stretch.start
        for step in range(share):
            last = step == share - 1
            end = stretch.end if last else here + edge - 1
            begin = here if here == 1 else max(1, here - lead_in)
            out.append(Slice(0, begin, here, end))
            here = end + 1
        at = stretch.end + 1
    if at <= frames:
        out.append(Slice(0, at, at, frames, blank=True))
    for index, piece in enumerate(out):
        piece.index = index
    return out


def split(frames: int, parts: int, lead_in: int = 60, chunk: int = 0) -> list[Slice]:
    """Divide a video into shares that can be matted on different machines, or in turn."""
    if frames <= 0:
        raise ValueError("the video has no frames to divide")
    parts = pieces_for(frames, parts, chunk)
    if parts == 1:
        return [Slice(0, 1, 1, frames)]
    edge, out, at = frames // parts, [], 1
    for index in range(parts):
        last = index == parts - 1
        end = frames if last else at + edge - 1
        out.append(Slice(index, max(1, at - (lead_in if index else 0)), at, end))
        at = end + 1
    return out


# --------------------------------------------------------------------------- putting it back

def finished(path: str | Path, wanted: int) -> bool:
    """Whether a piece from an earlier run is there and the right length to trust."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    return frame_count(path) == wanted


def frame_count(path: str | Path) -> int:
    """How many frames a file holds.

    The header is asked first. Counting every frame means decoding the whole file - measured
    at forty seconds for a chunk of 8K on a local disk, and several minutes over a network -
    and on restart that was paid for every finished chunk before any work resumed. On a
    long film it added up to well over an hour of doing nothing.

    A file written properly records its own frame count, and every chunk here is written by
    ffmpeg with an exact ``-frames:v``. One cut short by a crash has no such record, and only
    then is it worth counting the hard way - which is exactly the file whose length is in
    doubt.
    """
    said = paths.run_tool([
        paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=nb_frames", "-of", "default=nk=1:nw=1", str(path)],
        timeout=300)
    digits = re.search(rb"\d+", said.stdout or b"")
    if digits and int(digits.group()) > 0:
        return int(digits.group())
    return counted_frames(path)


def counted_frames(path: str | Path) -> int:
    """Frames counted by decoding the lot. Slow, and right even for a half-written file."""
    probe = paths.run_tool([
        paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames", "-of", "default=nk=1:nw=1", str(path)],
        timeout=1800)
    digits = re.search(rb"\d+", probe.stdout or b"")
    return int(digits.group()) if digits else 0


def _ffmpeg(args: list[str], timeout: float = 7200) -> None:
    done = paths.run_tool([paths.tool("ffmpeg"), "-hide_banner", "-v", "error", "-y", *args],
                          timeout=timeout)
    if done.returncode:
        tail = (done.stderr or b"").decode("utf-8", "replace")[-600:]
        raise Unavailable(f"ffmpeg failed:\n{tail}")


def _ffmpeg_watched(args: list[str], frames: int, along: Callable[[float, int], None],
                    timeout: float = 7200) -> None:
    """The same, but saying how far along it is.

    Re-encoding a chunk of 8K takes minutes, and doing it in silence is why the window used to
    sit on whatever it last said while work was plainly going on. ffmpeg will report its own
    position if asked, so it is asked.
    """
    process = subprocess.Popen(
        [paths.tool("ffmpeg"), "-hide_banner", "-v", "error", "-y", "-nostats",
         "-progress", "pipe:1", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=paths.NO_WINDOW)
    try:
        for line in process.stdout:
            name, _, value = line.strip().partition("=")
            if name == "frame" and value.isdigit():
                at = int(value)
                along(min(1.0, at / frames) if frames else 0.0, at)
        process.wait(timeout=timeout)
    finally:
        if process.poll() is None:
            process.kill()
    if process.returncode:
        tail = (process.stderr.read() or "")[-600:]
        raise Unavailable(f"ffmpeg failed:\n{tail}")


# What to encode the mask pieces with, worked out per frame size and remembered.
#
# Not libx264, which is the reflex and is wrong here: BlueShelf's ffmpeg is built LGPL, with
# no --enable-gpl, precisely so that shipping it obliges nothing about this program's own
# source. x264 and x265 are GPL and are therefore not in it, and asking for one fails with
# "Unknown encoder".
#
# The size matters as much as the name. NVENC's H.264 encoder stops at 4096 pixels across, so
# it takes a 256-pixel test frame happily and then refuses an 8K mask - which is the only size
# that was ever going to be asked of it. So each candidate is tried on one real frame at the
# real resolution, and nothing is trusted for being listed.
_MASK_ENCODERS: dict[tuple, list[str]] = {}
_MASK_CANDIDATES = (
    # Handles 8K, and the machines doing this work have the card by definition.
    ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "22", "-b:v", "0"],
    ["-c:v", "av1_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "24", "-b:v", "0"],
    ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0"],
    # No card, or a frame it will not take: these are software and BSD licensed.
    ["-c:v", "libsvtav1", "-crf", "30", "-preset", "8"],
    ["-c:v", "libopenh264", "-b:v", "24M"],
    # Lossless and native. Enormous, but it has never not been there.
    ["-c:v", "ffv1", "-level", "3"],
)


def mask_encoder(width: int, height: int, for_players: bool = False) -> list[str]:
    """The ffmpeg arguments to write a mask segment of this size.

    ``for_players`` is for a cut-out that will be played as it stands, by a browser or a
    headset: H.264 first, because every one of them can decode it and not all can decode
    HEVC. It is decided per job and never by size alone - a job's chunks must all agree on
    codec to be joined, so one already under way must not change halfway.
    """
    size = (int(width), int(height))
    key = (*size, for_players)
    if key in _MASK_ENCODERS:
        return _MASK_ENCODERS[key]
    order = _MASK_CANDIDATES
    if for_players:
        order = (tuple(c for c in _MASK_CANDIDATES if "264" in c[1])
                 + tuple(c for c in _MASK_CANDIDATES if "264" not in c[1]))
    tried = []
    for candidate in order:
        trial = paths.run_tool(
            [paths.tool("ffmpeg"), "-hide_banner", "-v", "error", "-f", "lavfi",
             "-i", f"color=c=white:s={size[0]}x{size[1]}:r=30:d=1", "-frames:v", "1",
             *candidate, "-pix_fmt", "yuv420p", "-f", "null", "-"], timeout=300)
        if trial.returncode == 0:
            _MASK_ENCODERS[key] = candidate
            return candidate
        tried.append(candidate[1])
    raise Unavailable(
        f"This copy of ffmpeg could not write a {size[0]}x{size[1]} mask with any of: "
        + ", ".join(tried) + ".")


def prepare_segment(segment: str | Path, piece: Slice, into: str | Path,
                    along: Callable[[float, int], None] | None = None,
                    size: tuple[int, int] | None = None) -> Path:
    """Turn one machine's raw output into a segment that can be joined to the others.

    Two things are wrong with it as it arrives. It begins with the warm-up frames - the model
    carries state from frame to frame, so a share started cold in the middle of a scene gets
    its first second or two wrong, and mattes a little ahead of its share so that part can be
    thrown away. And it may be a frame or two off the length that was asked for, because
    seeking into a long video lands near the mark rather than on it.

    A frame or two adrift on its own is invisible. Left alone across several segments it is
    not: every frame a segment is short pulls the whole rest of the matte forward, and by the
    end the cut-out is chasing the picture. So the length is measured and made exact here.

    Everything goes through the same encoder on the way out, whether it needed changing or
    not, because joining segments without re-encoding requires them to agree on codec and
    settings - and the machine that produced the first one may not have had the same graphics
    card as the machine that produced the second.
    """
    into = Path(into)
    have, drop, want = frame_count(segment), piece.lead_in, piece.kept
    chain = []
    if drop > 0:
        chain.append(rf"select=gte(n\,{drop})")
    chain.append("setpts=N/FRAME_RATE/TB")
    short = want - max(0, have - drop)
    if short > 0:
        # Hold the last frame. A cut-out that pauses for a frame is invisible; one that
        # slides out of step with the picture is not.
        chain.append(f"tpad=stop_mode=clone:stop={short}")
    have_size = _frame_size(segment)
    if size and tuple(size) != have_size:
        # Every chunk of a job must be one size or they will not join. Normally this is a
        # no-op - VRAutoMatte hands them over at the size they were worked at - but a
        # machine with a different card or setting may not, and this is where that ends.
        chain.append(f"scale={size[0]}:{size[1]}:flags=area")
    out_size = tuple(size) if size else have_size
    args = ["-i", str(segment), "-vf", ",".join(chain), "-frames:v", str(want), "-an",
            *mask_encoder(*out_size, for_players=bool(size)), "-pix_fmt", "yuv420p",
            str(into)]
    if along is None:
        _ffmpeg(args)
    else:
        _ffmpeg_watched(args, want, along)
    return into


def _frame_size(path: str | Path) -> tuple[int, int]:
    probe = paths.run_tool([
        paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height", "-of", "csv=p=0:s=x", str(path)], timeout=180)
    text = (probe.stdout or b"").decode("utf-8", "replace").strip().split("x")
    try:
        return int(text[0]), int(text[1])
    except (IndexError, ValueError):
        raise Unavailable(f"ffprobe could not measure {Path(path).name}.")


def exact_fps(path: str | Path, fallback: float) -> str:
    """The frame rate as the file itself states it - "60000/1001", not "59.940060".

    A decimal rate lets ffmpeg choose its own nearby time base: 59.940060 becomes 2997/50
    with a 1/11988 time base, while segments cut from the source carry 60000/1001 and
    1/60000. Joining the two without re-encoding then reads every real packet's timing in
    the wrong units, and the mask comes out five times too long - every frame present, in
    order, and useless, because the packer seeks the mask by time and lands nowhere near
    the frame it asked for.
    """
    try:
        said = paths.run_tool([
            paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate", "-of", "default=nk=1:nw=1", str(path)],
            timeout=60)
        rate = (said.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    except Exception:  # noqa: BLE001 - a rate we cannot read is not worth failing over
        rate = []
    found = re.fullmatch(r"(\d+)/(\d+)", rate[0].strip()) if rate else None
    if found and int(found.group(1)) and int(found.group(2)):
        return found.group(0)
    return f"{fallback:.6f}"


def blank_segment(job: Job, frames: int, into: str | Path,
                  along: Callable[[float, int], None] | None = None) -> Path:
    """A stretch nobody needed matted: an all-white mask, meaning keep the whole picture.

    White is opaque, so the scene plays as an ordinary VR video through the talking and the
    room only appears once the cut-out starts. Black would be the other way round and would
    make the performer vanish, which is the one outcome nobody wants.

    Encoded exactly like a real segment so the two can be joined without re-encoding.
    """
    into = Path(into)
    # No duration on the source: it runs forever and -frames:v decides the length. With a
    # d= on it the clip stops there instead, and every blank stretch came out one second
    # long however many frames were asked for - which would slide the mask out of step with
    # the picture for the whole rest of the video.
    w, h = mask_size_of(job)
    args = ["-f", "lavfi", "-i",
            f"color=c=white:s={w}x{h}:r={exact_fps(job.source, job.fps)}",
            "-frames:v", str(frames), "-an",
            *mask_encoder(w, h, for_players=job.small_mask), "-pix_fmt", "yuv420p", str(into)]
    if along is None:
        _ffmpeg(args)
    else:
        _ffmpeg_watched(args, frames, along)
    return into


def build_matte(job: Job, pieces: list[Slice], obtain: Callable[[Slice], Path],
                work_dir: str | Path, report: Progress) -> Path:
    """Put the finished mask together out of the pieces, whoever made them.

    ``obtain`` hands back the segment for a piece that needed matting - by doing it here, or
    by finding what another machine left in the shared folder. Blank pieces are made on the
    spot, since an all-white clip costs nothing.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    matte = work_dir / "matte.mp4"
    total = sum(p.kept for p in pieces)
    # Before anything is fetched or made. This used to be asked only after every piece had
    # been obtained, so a finished mask sitting right there did not stop the pieces it was
    # made from being matted all over again - hours of work to arrive at a file that was
    # already on disk.
    if finished(matte, total):
        report("The joined mask was already made", 0.99, f"{total:,} frames")
        return matte
    made: list[Path] = []
    whole = max(1, len(pieces))
    for at, piece in enumerate(sorted(pieces, key=lambda p: p.keep_from)):
        if piece.blank:
            spare = work_dir / f"blank-{piece.index:03d}.mp4"
            if finished(spare, piece.kept):
                made.append(spare)
                continue

            def filling(share: float, frame: int, at=at, piece=piece) -> None:
                # Kept under the figure the join reports, so the bar does not reach the end
                # and then step back when the last piece is written.
                report(f"Filling in a stretch with no action ({at + 1} of {whole})",
                       0.95 * (at + share) / whole, f"frame {frame:,} of {piece.kept:,}")

            filling(0.0, 0)
            made.append(blank_segment(job, piece.kept, spare, filling))
        else:
            made.append(Path(obtain(piece)))
    report("Joining the pieces", 0.98, f"{len(made)} of them")
    join(made, matte)
    check_timing(matte, total, job.fps)
    return matte


def check_timing(matte: str | Path, frames: int, fps: float) -> None:
    """Refuse a mask whose frames are there but whose clock is wrong.

    Joining without re-encoding takes the time base from the first piece, so a blank
    stretch written at a different one made every real piece's timing be read in the wrong
    units. The mask came out five times too long: every frame present, in order, and
    useless, because the packer seeks it by time and landed nowhere near the frame it
    asked for - which shows up as a copy with no cut-out at all, hours later.

    Nothing about that is visible in a frame count, so the length is checked instead.
    """
    if frames <= 0 or fps <= 0:
        return
    said = paths.run_tool([
        paths.tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", str(matte)], timeout=60)
    found = re.search(rb"[\d.]+", said.stdout or b"")
    if not found:
        return
    measured, wanted = float(found.group()), frames / fps
    if abs(measured - wanted) > max(1.0, wanted * 0.01):
        raise Unavailable(
            f"The joined mask is {measured:,.0f} seconds long where {frames:,} frames at "
            f"{fps:.3f} a second should be {wanted:,.0f}. Its pieces were written with "
            f"different time bases, so the mask would not line up with the picture. "
            f"Delete {Path(matte).name} and let it be joined again.")


def join(segments: list[str | Path], into: str | Path,
         along: Callable[[float, int], None] | None = None) -> Path:
    """Stitch the segments back into one video, in order.

    ``along`` is given the share done and the frame reached. Joining the finished copy moves
    twenty-odd gigabytes and takes a quarter of an hour, and without this the helper sat on
    one line the whole time with no way to tell working from stuck.
    """
    into = Path(into)
    if len(segments) == 1:
        if Path(segments[0]) != into:
            shutil.copy2(segments[0], into)
        return into
    listing = into.with_suffix(".txt")
    listing.write_text(
        "".join("file '{}'\n".format(str(Path(s).resolve()).replace("'", "'\\''"))
                for s in segments), encoding="utf-8")
    try:
        # Whatever the pieces carry. This used to drop the sound, because it only ever
        # joined masks - which have none - and then it was used to join pictures too.
        args = ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(into)]
        if along is None:
            _ffmpeg(args)
        else:
            _ffmpeg_watched(args, sum(frame_count(s) for s in segments), along)
    finally:
        try:
            listing.unlink()
        except OSError:
            pass
    return into


# --------------------------------------------------------------------------- scenes of a film

# Scenes marked while watching are cut out of the film into a clip of their own, which then
# goes through everything above like any other video. The clip is kept out of sight - a
# folder whose name starts with a dot is one the library never looks in - and the passthrough
# copy made from it goes beside the film, where it belongs.
SCENES_FOLDER = re.compile(r"^\..+\.scenes$")


def scenes_folder(film: str | Path) -> Path:
    return Path(film).parent / f".{Path(film).stem}.scenes"


def output_folder(source: str | Path) -> Path:
    """Where a finished copy goes: beside what it was made from - or, for a clip of a film's
    scenes, beside the film the scenes came from."""
    folder = Path(source).parent
    return folder.parent if SCENES_FOLDER.match(folder.name) else folder


def scene_clip_path(film: str | Path, name: str) -> Path:
    """The film's own name first, so the lens and layout read from it still read from the clip."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip(" .") or "scenes"
    return scenes_folder(film) / f"{Path(film).stem} - {safe[:60]}.mp4"


def cut_scenes(film: str | Path, ranges: list[tuple[float, float]], into: str | Path,
               report: Callable[[str, float, str], None] = lambda _s, _f, _d: None) -> Path:
    """The chosen scenes of a film, in the chosen order, joined into one clip - copied, not
    re-encoded.

    A copy can only start on a key frame, so each scene starts at the key frame at or before
    where it was marked: a second or two early at most, never late. Its end is exact.
    Re-encoding would put the start exactly where it was marked too, at the cost of minutes of
    7K encoding per scene and one more generation of compression before packing adds its own.
    """
    into = Path(into)
    if not ranges:
        raise Unavailable("No scenes were chosen.")
    into.parent.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for n, (start, end) in enumerate(ranges):
            if end - start <= 0.5:
                raise Unavailable(f"Scene {n + 1} is too short to cut.")
            report(f"Cutting scene {n + 1} of {len(ranges)} out of the film", n / len(ranges),
                   f"{_span(start)} to {_span(end)}")
            part = into.with_name(f"{into.stem}.part{n:02d}.mp4")
            _ffmpeg(["-ss", f"{start:.3f}", "-i", str(film), "-t", f"{end - start:.3f}",
                     "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                     "-avoid_negative_ts", "make_zero", str(part)])
            if not part.is_file() or frame_count(part) <= 0:
                raise Unavailable(f"Cutting scene {n + 1} produced nothing.")
            parts.append(part)
        if len(parts) == 1:
            os.replace(parts.pop(), into)       # one scene: it is the clip, no second copy
        else:
            report("Joining the scenes into one clip", 0.95, f"{len(parts)} of them")
            partial = into.with_name(into.stem + ".joining.mp4")
            join(parts, partial)
            os.replace(partial, into)
    finally:
        for part in parts:
            try:
                part.unlink()
            except OSError:
                pass
    report("Scenes cut", 1.0, into.name)
    return into


# --------------------------------------------------------------------------- the simple case

def make_here(root: str | Path, job: Job, report: Progress,
              should_stop: Callable[[], bool] = lambda: False) -> Path:
    """The whole video on this machine, start to finish, the way VRAutoMatte does it itself."""
    run = Run(root, job.spec(mode="whole", output=job.output))
    try:
        for message in run.lines():
            if should_stop():
                run.stop()
                raise InterruptedError("Cancelled.")
            if message.get("event") == "progress":
                report(message.get("stage", ""), float(message.get("share", 0.0)),
                       _detail(message))
    finally:
        run.cleanup()
    if run.error:
        raise Unavailable(run.error)
    return Path(run.result["output"])


def work_folder(job: Job) -> Path:
    """Where a job's half-finished chunks live: beside the copy it is making.

    Named after the **source**, not the output. It used to be named after the output, and
    the output name carries the lens angle - so nudging that spinner by one notch pointed
    the whole job at an empty folder and hours of finished chunks vanished from view. The
    lens angle has nothing to do with the mattes; it only affects the final conversion. The
    folder now depends on nothing but which video is being worked on.
    """
    return Path(job.output).parent / f".{Path(job.source).stem}.matte"


def older_work_folders(job: Job) -> list[Path]:
    """Folders earlier versions used for this same video, so their chunks are not lost."""
    beside = Path(job.output).parent
    stem = Path(job.source).stem
    if not beside.is_dir():
        return []
    try:
        return sorted(f for f in beside.glob(f".{stem}*.work") if f.is_dir())
    except OSError:
        return []


def gather_older_work(job: Job, pieces: list[Slice]) -> int:
    """Bring chunks from anywhere an older version put them into the folder in use now.

    Two places, because there were two: the working folder named after the output, and -
    when the work was shared between machines - a folder inside the shared job itself. Both
    hold hours of matting each, and neither is where anything looks any more.
    """
    into = work_folder(job)
    brought = 0
    for old in older_work_folders(job):
        if old.resolve() == into.resolve():
            continue
        for piece in pieces:
            if piece.blank:
                continue
            here = part_file(into, piece)
            if finished(here, piece.kept):
                continue
            # Named part-007.mp4 when one machine made it, 7.mp4 when a shared job did.
            for there in (part_file(old, piece),
                          *sorted(old.glob(f"jobs/*/out/{piece.index}.mp4"))):
                if not finished(there, piece.kept):
                    continue
                into.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(there), str(here))
                    brought += 1
                except OSError:
                    continue
                break
    return brought


# What a chunk actually depends on. Change any of these and the chunks already matted are
# answers to a different question, so they cannot be carried over.
DECIDING = ("model", "detail", "stride", "projection", "roi", "smoothing")


def settings_file(job: Job) -> Path:
    return work_folder(job) / "settings.json"


def save_settings(job: Job, pieces: list[Slice]) -> None:
    """Record what this job was set to, next to the chunks it produces.

    Kept with the work rather than in the library, so it travels with the video and any
    machine picking the job up sees the same answer.
    """
    folder = work_folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    kept = {name: getattr(job, name) for name in DECIDING}
    kept.update({"source": job.source, "frames": job.frames, "fov": job.fov,
                 "crf": job.crf, "fps": job.fps, "mask_w": job.mask_w, "mask_h": job.mask_h,
                 "plan": [[p.index, p.start, p.keep_from, p.end, p.blank] for p in pieces]})
    try:
        settings_file(job).write_text(json.dumps(kept, indent=1), encoding="utf-8")
    except OSError:
        pass


def read_settings(job: Job) -> dict | None:
    """What this job was set to last time, if anything was written down."""
    try:
        return json.loads(settings_file(job).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def settings_differences(job: Job, pieces: list[Slice], saved: dict) -> list[str]:
    """How the settings now differ from the ones the existing chunks were made with.

    Only the differences that matter. The lens angle and the quality of the final copy are
    not in here: they change the copy, not the chunks, so they can be altered freely without
    any work being wasted.
    """
    found = []
    for name in DECIDING:
        was, now = saved.get(name), getattr(job, name)
        if was is not None and was != now:
            found.append(f"{name}: was {was}, now {now}")
    plan = [[p.index, p.start, p.keep_from, p.end, p.blank] for p in pieces]
    if saved.get("plan") and saved["plan"] != plan:
        was_chunks = len([p for p in saved["plan"] if not p[4]])
        found.append(f"the video is cut up differently: {was_chunks} chunks before, "
                     f"{len([p for p in pieces if not p.blank])} now")
    return found


def part_file(work_dir: str | Path, piece: Slice) -> Path:
    return Path(work_dir) / f"part-{piece.index:03d}.mp4"


def so_far(job: Job, pieces: list[Slice],
           work_dir: str | Path | None = None) -> tuple[list[Slice], int, int]:
    """Which chunks are already matted, and how much of the video they cover.

    Returns a plan where anything not yet matted is marked blank, so it can be built into a
    copy that plays now: passthrough through the parts that are done, and an ordinary VR
    scene through the rest.
    """
    folder = Path(work_dir or work_folder(job))
    plan, ready, frames = [], 0, 0
    for piece in pieces:
        if piece.blank or finished(part_file(folder, piece), piece.kept):
            if not piece.blank:
                ready += 1
                frames += piece.kept
            plan.append(piece)
        else:
            # Not done yet: an opaque stretch, which plays as the video itself.
            plan.append(replace(piece, blank=True))
    return plan, ready, frames


def partial_output(job: Job) -> str:
    """Where a copy made from part of the work goes - never over the finished one."""
    source = Path(job.output)
    stem = ALREADY.sub("", LENS.sub("", source.stem))
    return str(source.with_name(f"{stem}_sofar_FISHEYE{job.fov}_alpha.mp4"))


def cut_to(source: str | Path, keep: list[Slice], into: str | Path, width: int, height: int,
           fps: float, along: Callable[[float, int], None] | None = None) -> Path:
    """Copy just the chosen runs of frames out of a video, joined end to end.

    Each run is seeked to and read on its own, rather than filtering the whole file down to
    the parts wanted. Picking frames by number means decoding everything, including the
    two-thirds being thrown away - on a 52-minute 8K film that is most of the cost, and all
    of it wasted.

    Re-encoded rather than stream-copied: a copy starts at the nearest keyframe, which would
    put the picture a second or two out of step with the mask, and a mask out of step is
    worse than no mask. Each run's length is checked against what was asked for, because a
    picture that is short by even a frame drags everything after it out of line.
    """
    into = Path(into)
    if fps <= 0:
        raise Unavailable("The video does not say how fast it runs, so it cannot be cut.")
    total = sum(p.kept for p in keep)
    if finished(into, total):
        if along:
            along(1.0, total)
        return into                    # done by a run that stopped after this
    encoder = mask_encoder(width, height)
    done = [0]
    made = []
    scratch = into.parent / f"{into.stem}.parts"
    scratch.mkdir(parents=True, exist_ok=True)
    kept_scratch = False
    try:
        for order, piece in enumerate(keep):
            part = scratch / f"cut-{piece.index:03d}.mp4"
            if finished(part, piece.kept):
                # Cut out by an earlier attempt; reading 8K again to get the same frames
                # would be minutes thrown away.
                done[0] += piece.kept
                made.append(part)
                if along:
                    along(min(1.0, done[0] / max(1, total)), done[0])
                continue

            def onward(share: float, at: int, piece=piece) -> None:
                if along:
                    along(min(1.0, (done[0] + at) / max(1, total)), done[0] + at)

            # The sound comes too. Cut to the same length by time as the picture is by
            # frames, and re-encoded rather than copied so the pieces can be joined without
            # the audio drifting at every seam.
            args = ["-ss", f"{(piece.keep_from - 1) / fps:.6f}", "-i", str(source),
                    "-t", f"{piece.kept / fps:.6f}",
                    "-map", "0:v:0", "-map", "0:a?",
                    "-frames:v", str(piece.kept),
                    *encoder, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", str(part)]
            if along is None:
                _ffmpeg(args)
            else:
                _ffmpeg_watched(args, piece.kept, onward)
            got = frame_count(part)
            if got < piece.kept:
                # The tail of a file can come up short. Hold the last frame rather than let
                # everything after this run slide.
                padded = scratch / f"cut-{piece.index:03d}.fit.mp4"
                _ffmpeg(["-i", str(part), "-vf",
                         f"tpad=stop_mode=clone:stop={piece.kept - got}",
                         "-frames:v", str(piece.kept), "-map", "0:v:0", "-map", "0:a?",
                         *encoder, "-pix_fmt", "yuv420p", "-c:a", "copy", str(padded)])
                padded.replace(part)
            done[0] += piece.kept
            made.append(part)
        join(made, into)
        kept_scratch = True
    finally:
        # Only once the joined result exists. A run that died half way through has its
        # finished stretches waiting for the next attempt.
        if kept_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    return into


def make_so_far(root: str | Path, job: Job, pieces: list[Slice], report: Progress,
                work_dir: str | Path | None = None) -> Path:
    """Build a watchable copy out of nothing but the chunks that are done.

    Only those parts appear in it. The stretches not matted yet are left out altogether
    rather than filled in: filling them would mean encoding hours of 8K to produce the
    original video back again, and the whole point of looking part-way through is to see the
    cut-out. So the copy is as long as the work, not as long as the film.

    Nothing is re-matted and nothing is moved. The real job carries on untouched.
    """
    folder = Path(work_dir or work_folder(job))
    done = [p for p in pieces
            if not p.blank and finished(part_file(folder, p), p.kept)]
    if not done:
        raise Unavailable("None of the chunks are finished yet, so there is nothing to make.")
    done.sort(key=lambda p: p.keep_from)
    partial = replace(job, output=partial_output(job))

    covered = sum(p.kept for p in done)
    matte = folder / "sofar-matte.mp4"
    if finished(matte, covered):
        report("The mask for those parts was already joined", 0.04, f"{covered:,} frames")
    else:
        report("Joining the finished chunks", 0.02, f"{len(done)} of them")
        join([part_file(folder, p) for p in done], matte)

    picture = folder / "sofar-picture.mp4"

    def cutting(share: float, at: int) -> None:
        report("Taking those parts out of the video", 0.05 + 0.45 * share,
               f"frame {at:,} of {sum(p.kept for p in done):,}")

    cutting(0.0, 0)
    cut_to(job.source, done, picture, job.width, job.height, job.fps, cutting)

    made = pack_in_pieces(root, partial, picture, matte, partial.output, report, folder,
                          chunk=job.pack_chunk)
    for leftover in (matte, picture):      # only now, with something to show for them
        try:
            leftover.unlink()
        except OSError:
            pass
    return made


def pack_piece(root: str | Path, job: Job, picture: str | Path, matte: str | Path,
               piece: Slice, into: str | Path, report: Progress) -> Path:
    """Project and pack one range of frames into a finished piece of the copy."""
    run = Run(root, job.spec(mode="finish", source=str(picture), matte=str(matte),
                             output=str(into), start=piece.start, end=piece.end,
                             work_dir=str(Path(into).parent)))
    try:
        for message in run.lines():
            if message.get("event") == "progress":
                report(message.get("stage", ""), float(message.get("share", 0.0)),
                       _detail(message))
    finally:
        run.cleanup()
    if run.error:
        raise Unavailable(run.error)
    return Path(into)


def frame_timing(source: str | Path, sample: int = 400) -> tuple[str, str]:
    """Whether a video's frames are evenly spaced: "cfr", "vfr", or "unknown".

    Matting writes a mask at one steady rate and the packer lays it over the picture by
    timestamp. That only lines up while the picture's own frames are evenly spaced. Some
    studios ship variable frame rate, and then the mask drifts further out of step the
    longer it plays - a ghost trailing the performer, after hours of work, with nothing
    having gone wrong anywhere it could be seen.

    The header is not enough on its own: r_frame_rate and avg_frame_rate differ on plenty
    of honest files. The packets are asked instead, at the start and in the middle, and the
    gaps between them compared.
    """
    gaps: list[float] = []
    for where in ("%+#{}".format(sample), "50%+#{}".format(sample)):
        try:
            said = paths.run_tool([
                paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                "-read_intervals", where, str(source)], timeout=120)
        except Exception:  # noqa: BLE001 - a file we cannot probe is not a file we can judge
            return "unknown", "the frame times could not be read"
        times = sorted(float(t) for t in re.findall(rb"[\d.]+", said.stdout or b"")
                       for t in [t.decode()] if t not in ("", "."))
        gaps += [b - a for a, b in zip(times, times[1:]) if b > a]
    if len(gaps) < 20:
        return "unknown", "too few frames to tell"
    gaps.sort()
    middle = gaps[len(gaps) // 2]
    if middle <= 0:
        return "unknown", "the frame times made no sense"
    odd = sum(1 for g in gaps if abs(g - middle) > 0.25 * middle)
    if odd > len(gaps) * 0.02:
        return "vfr", (f"{odd} of {len(gaps)} gaps between frames differ from the usual "
                       f"{middle * 1000:.1f} ms by more than a quarter")
    return "cfr", f"evenly spaced, {1 / middle:.3f} a second"


def packable_frames(matte: str | Path, source: str | Path) -> int:
    """How many frames can actually be packed: the shorter of the mask and the picture.

    These are not always the same. The mask is joined from pieces and came out one frame
    longer than the video it was made from, so the last piece was planned for 291 frames
    while the packer - which clamps to the picture's length - could only ever make 290.
    A piece that cannot reach the length asked of it is never accepted as finished, so it
    was handed out again, and again: the queue never completed, the join never ran, and the
    job span all night on one piece without ever saying anything was wrong.
    """
    have = frame_count(matte)
    try:
        picture = frame_count(source)
    except Exception:  # noqa: BLE001 - an unreadable picture is the packer's problem to report
        return have
    return min(have, picture) if picture > 0 else have


def tail_pieces(frames: int, chunk: int = 0) -> list[Slice]:
    """Cut the tail into ranges. Nothing here carries state, so there is no warm-up."""
    if frames <= 0:
        raise ValueError("there is nothing to pack")
    step = chunk or frames
    return [Slice(n, a, a, min(a + step - 1, frames))
            for n, a in enumerate(range(1, frames + 1, step))]


def pack_in_pieces(root: str | Path, job: Job, source: str | Path, matte: str | Path,
                   output: str | Path, report: Progress, work_dir: str | Path,
                   chunk: int = 0) -> Path:
    """Project and pack, a frame range at a time, and join the pieces.

    Nothing in this stage depends on the frame before it - unlike matting, where the model
    carries state - so a range can be done on its own, by any machine, in any order, with no
    run-up and nothing lost. Doing it in pieces means a failure costs one piece, and it is
    what lets the tail of a job be shared out the way the matting already is.
    """
    work_dir, output = Path(work_dir), Path(output)
    frames = packable_frames(matte, source)
    if frames <= 0:
        raise Unavailable("The mask could not be read, so there is nothing to pack.")
    edges = tail_pieces(frames, chunk)
    made, done = [], 0
    for piece in edges:
        part = work_dir / f"packed-{piece.index:03d}.mp4"
        if finished(part, piece.kept):
            report(f"Piece {piece.index + 1} of {len(edges)} was already packed",
                   (done + piece.kept) / frames, "")
        else:
            def along(stage: str, share: float, detail: str, piece=piece, done=done) -> None:
                report(f"Piece {piece.index + 1} of {len(edges)} - {stage}",
                       (done + share * piece.kept) / frames, detail)

            run = Run(root, job.spec(mode="finish", source=str(source), matte=str(matte),
                                     output=str(part), start=piece.start, end=piece.end,
                                     work_dir=str(work_dir)))
            try:
                for message in run.lines():
                    if message.get("event") == "progress":
                        along(message.get("stage", ""), float(message.get("share", 0.0)),
                              _detail(message))
            finally:
                run.cleanup()
            if run.error:
                raise Unavailable(run.error)
        made.append(part)
        done += piece.kept
    if len(made) == 1:
        made[0].replace(output)
    else:
        report("Joining the finished pieces", 0.99, f"{len(made)} of them")
        join(made, output)
        for part in made:
            try:
                part.unlink()
            except OSError:
                pass
    return output


def measure_link(folder: str | Path, megabytes: int = 256) -> dict:
    """How fast this machine can write to and read from a folder, in MB a second.

    The number that decides where the tail of a job should run. Matting barely touches the
    network - a chunk reads a few hundred megabytes over a quarter of an hour - but
    projecting and packing move tens of gigabytes, and a machine on a slow link spends most
    of that stage waiting rather than working.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / f".speed-{uuid.uuid4().hex[:8]}"
    block = os.urandom(1024 * 1024)
    try:
        began = time.monotonic()
        with open(probe, "wb", buffering=0) as out:
            for _ in range(megabytes):
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        writing = max(time.monotonic() - began, 0.001)

        began = time.monotonic()
        with open(probe, "rb", buffering=0) as back:
            while back.read(4 * 1024 * 1024):
                pass
        reading = max(time.monotonic() - began, 0.001)
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return {"write_mbs": round(megabytes / writing, 1),
            "read_mbs": round(megabytes / reading, 1)}


def measure_matting(root: str | Path, job: Job, frames: int = 150,
                    report: Progress = lambda _s, _f, _d: None) -> float:
    """How many frames a second this machine mattes, at the settings in use.

    Measured rather than guessed from the card's name: it depends on the detail setting, the
    model, and how much of the frame the performer fills. A short run of real frames from
    the real video answers it properly.
    """
    middle = max(1, job.frames // 2)
    piece = Slice(0, middle, middle, min(job.frames, middle + frames - 1))
    into = Path(tempfile.gettempdir()) / f"blueshelf-speed-{uuid.uuid4().hex[:8]}.mp4"
    began = time.monotonic()
    try:
        matte_slice(root, job, piece, into, report)
        took = max(time.monotonic() - began, 0.001)
    finally:
        try:
            into.unlink()
        except OSError:
            pass
    return round(piece.kept / took, 2)


def make_in_pieces(root: str | Path, job: Job, pieces: list[Slice], report: Progress,
                   should_stop: Callable[[], bool] = lambda: False,
                   work_dir: str | Path | None = None,
                   note: Callable[[str], None] = lambda _m: None) -> Path:
    """A video matted here in pieces, because only parts of it needed doing.

    The whole-video path is left to VRAutoMatte itself; this one exists for a job where some
    stretches are skipped, and it shares its machinery with the one that spreads work over
    several machines.
    """
    work_dir = Path(work_dir or work_folder(job))
    work_dir.mkdir(parents=True, exist_ok=True)
    save_settings(job, pieces)
    wanted = [p for p in pieces if not p.blank]
    done = [0]
    furthest = [0.0]
    tell = report

    def onward(stage: str, share: float, detail: str) -> None:
        """Report, but never backwards.

        The figures come from several places - the model's own count, ffmpeg's frame counter,
        the tally of finished chunks - and they do not always agree to the last decimal. A bar
        that slips back a hair reads as something having gone wrong.
        """
        furthest[0] = max(furthest[0], min(1.0, share))
        tell(stage, furthest[0], detail)

    report = onward

    def one(piece: Slice) -> Path:
        order = wanted.index(piece) + 1
        whole = max(1, len(wanted))

        def step(share: float) -> float:
            return (done[0] + min(1.0, share)) / whole

        def along(stage: str, share: float, detail: str) -> None:
            # "Complete" is VRAutoMatte's word for finishing one chunk, and passing it
            # through unchanged made a job of forty chunks announce itself as done at the
            # end of the first one.
            if stage.strip().lower().startswith("complete"):
                stage = "matted"
            # Matting is most of a chunk but not all of it; the rest is the tidying below.
            report(f"Chunk {order} of {whole} - {stage}", step(share * 0.88), detail)

        cut = part_file(work_dir, piece)
        if finished(cut, piece.kept):
            # Left behind by a run that stopped part-way. Hours of work; not done again.
            note(f"Chunk {order} of {whole} was already done.")
            report(f"Chunk {order} of {whole} - already done", step(1.0), "")
            done[0] += 1
            return cut
        raw = work_dir / f"part-{piece.index:03d}.raw.mp4"
        matte_slice(root, job, piece, raw, along, should_stop)

        def tidying(share: float, at: int) -> None:
            report(f"Chunk {order} of {whole} - tidying up",
                   step(0.88 + 0.12 * share), f"frame {at:,} of {piece.kept:,}")

        tidying(0.0, 0)
        prepare_segment(raw, piece, cut, tidying,
                        size=mask_size_of(job) if job.small_mask else None)
        try:
            raw.unlink()
        except OSError:
            pass
        done[0] += 1
        note(f"Chunk {order} of {whole} done.")
        # done[0] has already moved on, so the share is read straight off it. Going through
        # step() here would count this chunk twice and the bar would jump backwards.
        report(f"Chunk {order} of {whole} done", done[0] / whole, "")
        return cut

    matte = build_matte(job, pieces, one, work_dir, onward)
    if job.make == "matte":
        made = keep_cutout(job, matte, onward)
    else:
        made = pack_in_pieces(root, job, job.source, matte, job.output, onward, work_dir,
                              chunk=job.pack_chunk)
        if job.keep_mask:
            keep_cutout(job, matte, onward)
    # Only once there is something to show for it. A run that failed or was stopped leaves
    # its finished chunks where they are, so starting again carries on rather than restarts.
    tidy_after(job, work_dir)
    return str(made)


def cutout_file(job: Job) -> Path:
    """Where a film's standalone cut-out is kept: beside its chunks, named after the model
    that made it, so a second model's cut-out sits next to the first instead of replacing
    it - which is the whole point of being able to pick between them."""
    return work_folder(job) / f"mask-{job.model}.mp4"


def result_of(job: Job) -> Path:
    """What a finished job leaves behind: the packed copy, or the cut-out on its own."""
    return cutout_file(job) if job.make == "matte" else Path(job.output)


def keep_cutout(job: Job, matte: str | Path, report: Progress = lambda _s, _f, _d: None) -> Path:
    """Keep the joined matte as the film's standalone cut-out, for BlueShelf's own player.

    A small matte is already the right thing, and only needs its index moved to the front
    so a player can start it without reading to the end first. A full-size one, from a job
    filed before masks were kept small, has to be shrunk - which means decoding every frame
    of it, the slow part of all this, and the reason new jobs no longer make them.
    """
    matte, target = Path(matte), cutout_file(job)
    want = working_mask_size(job.width, job.height, job.detail, job.sbs)
    if _frame_size(matte) == want:
        report("Keeping the cut-out", 0.99, "")
        partial = target.with_name(target.stem + ".incoming.mp4")
        _ffmpeg(["-i", str(matte), "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart",
                 str(partial)])
        os.replace(partial, target)
    else:
        shrink_mask(matte, target, want, exact_fps(job.source, job.fps), report)
    try:
        matte.unlink()
    except OSError:
        pass
    return target


def shrink_mask(matte: str | Path, into: str | Path, size: tuple[int, int], rate: str,
                report: Progress = lambda _s, _f, _d: None) -> Path:
    """A small cut-out made from a full-size one.

    Frames are counted rather than timestamps trusted: a joined mask on the wrong clock is
    what once stretched a mask to five times its length, and counting cannot go wrong that
    way. Keyframes every half second, so a seek in the player has little to decode.
    """
    into = Path(into)
    w, h = size
    one = w * h
    total = max(1, frame_count(matte))
    partial = into.with_name(into.stem + ".incoming.mp4")
    encoder = subprocess.Popen(
        [paths.tool("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{w}x{h}", "-r", rate, "-i", "-", *mask_encoder(w, h, for_players=True),
         "-g", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(partial)],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=paths.NO_WINDOW)
    reader = subprocess.Popen(
        [paths.tool("ffmpeg"), "-v", "error", "-hwaccel", "auto", "-i", str(matte),
         "-vf", f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=paths.NO_WINDOW)
    count = 0
    try:
        while True:
            block = reader.stdout.read(one)
            if len(block) < one:
                break
            encoder.stdin.write(block)
            count += 1
            if count % 600 == 0:
                report("Making the cut-out small enough to play beside the film",
                       count / total, f"frame {count:,} of {total:,}")
    finally:
        reader.kill()
        encoder.stdin.close()
        encoder.wait()
    if count < total - 2:
        partial.unlink(missing_ok=True)
        raise Unavailable(f"The cut-out stopped at frame {count:,} of {total:,}.")
    os.replace(partial, into)
    return into


def tidy_after(job: Job, work_dir: str | Path) -> None:
    """Clear up once a job has produced something, keeping what is worth keeping.

    Small chunks are kept: they are cheap, and joining them again is how a standalone
    cut-out can be made later for a film that was only packed. Full-size ones from older
    jobs go, as they always did. A cut-out is never thrown away with them.
    """
    work_dir = Path(work_dir)
    if not work_dir.is_dir():
        return
    for item in work_dir.iterdir():
        name = item.name
        if name.startswith("mask"):
            continue
        spare = (name == "matte.mp4" or name.startswith("packed-") or ".raw" in name
                 or name.endswith(".incoming.mp4"))
        if spare or not job.small_mask:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except OSError:
                    pass
    if not job.small_mask and not any(work_dir.iterdir()):
        try:
            work_dir.rmdir()
        except OSError:
            pass


def _detail(message: dict) -> str:
    bits = []
    if message.get("frames"):
        bits.append(f"frame {message['frame']:,} of {message['frames']:,}")
    if message.get("fps"):
        bits.append(f"{message['fps']:g} fps")
    if message.get("eta"):
        bits.append(f"about {_span(message['eta'])} left")
    return " - ".join(bits)


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} hours"


def matte_slice(root: str | Path, job: Job, piece: Slice, into: str | Path, report: Progress,
                should_stop: Callable[[], bool] = lambda: False) -> Path:
    """One share of a video, matted here and left as a plain black-and-white segment."""
    run = Run(root, job.spec(mode="matte", output=str(into), start=piece.start, end=piece.end,
                             resume=False))
    try:
        for message in run.lines():
            if should_stop():
                run.stop()
                raise InterruptedError("Cancelled.")
            if message.get("event") == "progress":
                report(message.get("stage", ""), float(message.get("share", 0.0)),
                       _detail(message))
    finally:
        run.cleanup()
    if run.error:
        raise Unavailable(run.error)
    return Path(run.result["output"])


def pack(root: str | Path, job: Job, matte: str | Path, report: Progress,
         work_dir: str | Path | None = None) -> Path:
    """Turn the finished matte and the original video into the alpha file DeoVR reads.

    No graphics card is used here, so the machine that collects the segments can be any of
    them.
    """
    run = Run(root, job.spec(mode="finish", matte=str(matte), output=job.output,
                             work_dir=str(work_dir or Path(job.output).parent)))
    try:
        for message in run.lines():
            if message.get("event") == "progress":
                report(message.get("stage", ""), float(message.get("share", 0.0)),
                       _detail(message))
    finally:
        run.cleanup()
    if run.error:
        raise Unavailable(run.error)
    return Path(run.result["output"])
