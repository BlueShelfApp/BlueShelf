"""Runs a matting job inside VRAutoMatte's own Python, and reports back as it goes.

This file is the one piece of BlueShelf that does not run in BlueShelf. VRAutoMatte needs
PyTorch and a CUDA build to work, which together weigh several gigabytes - far more than the
rest of BlueShelf put together, and useless on a machine without an NVIDIA card. So instead of
bundling it, BlueShelf launches this script with VRAutoMatte's interpreter and talks to it over
its output.

Nothing here may import blueshelf: it is a different interpreter with a different set of
packages. Everything comes in through a JSON file named on the command line, and everything
goes back out as one JSON object per line on stdout.

  matte_worker.py <spec.json>

Modes:
  probe   - report whether this install can actually do the work, and on what card
  look    - read a video through cheaply and report where the action is
  matte   - produce the black-and-white matte for a frame range
  whole   - do the entire job here, straight to a finished alpha file
  finish  - turn a ready-made matte plus the original video into a DeoVR alpha file
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


def say(**fields) -> None:
    """One JSON object per line, flushed, so the reader sees it while the job is still running."""
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


def probe() -> dict:
    """What this machine can do - asked before anything long is started."""
    found = {"ok": False, "gpu": "", "vram_gb": 0.0, "torch": "", "cuda": False, "ffmpeg": False}
    try:
        from vrautomatte.utils.ffmpeg import check_ffmpeg
        found["ffmpeg"] = bool(check_ffmpeg())
    except Exception as exc:
        found["error"] = f"VRAutoMatte would not import: {exc}"
        return found
    try:
        import torch
        found["torch"] = torch.__version__
        found["cuda"] = bool(torch.cuda.is_available())
        if found["cuda"]:
            found["gpu"] = torch.cuda.get_device_name(0)
            found["vram_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / 1_073_741_824, 1)
    except Exception as exc:
        found["error"] = f"PyTorch would not load: {exc}"
        return found
    found["ok"] = found["ffmpeg"] and found["cuda"]
    if not found["cuda"]:
        found["error"] = ("No CUDA graphics card is visible, so matting would fall back to the "
                          "processor and take days rather than minutes.")
    elif not found["ffmpeg"]:
        found["error"] = "ffmpeg is not on the PATH."
    return found


# ── Finding the parts worth matting ──────────────────────────────────────────
#
# Most scenes open with several minutes of clothed talking. Matting that is wasted GPU time:
# there is nothing to cut out that anyone wants floating in their room. So the video is read
# through cheaply first and the stretches that look like action are found, and only those are
# matted. The rest becomes a blank mask, which costs nothing to make and plays as an ordinary
# VR scene.
#
# Two measures are taken, from the same frames, and BlueShelf can show either.
#
# **Exposure** asks a detector which parts of a body are on screen and whether they are covered.
# This is the one to use. It answers something close to the real question.
#
# **Skin** is what came first: how much of the frame is skin-coloured. It is a poor proxy and a
# worse one in VR, where a POV scene keeps a performer close to the lens throughout - measured
# on a real file, the conversation ran at a skin fraction of 0.20 and the peaks reached 0.46,
# and stretches the viewer knew were action measured lower than stretches that were not. It is
# kept as a fallback for when the detector is not installed.
#
# Neither is the truth, which is why BlueShelf draws the answer and lets it be corrected before
# hours of work are committed to it.

# Skin in YCbCr, the usual bounds. ffmpeg hands over yuv444p so no conversion is needed here.
SKIN_CB = (77, 127)
SKIN_CR = (133, 173)
SKIN_MIN_LUMA = 55     # a dark frame is not skin, whatever its colour says

# The detector's own input size, so nothing is resized twice.
SAMPLE_W = SAMPLE_H = 320
# How much of one eye to look at. A VR scene puts the action in front of the viewer, and the
# outer edge of the frame is stretched lens distortion that the detector was never trained on.
CENTRE = 0.6
# Of everything the detector can name, these are the ones that mean the scene has started.
# Bellies, armpits and faces are not news, and a covered breast is the opposite of news.
EXPOSED = frozenset({
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED",
})


def read_video(source: str, width: int, height: int, duration: float) -> dict:
    """Read the video through and report how much of each sampled frame is skin.

    One pass, and it talks the whole way. An earlier version walked the file twice - once with
    ffprobe for the keyframe times, then again for the pictures - and said nothing during the
    first walk, which on a long file looks exactly like a program that has hung. It also could
    not be cancelled, because cancelling only happens between messages and there were none.

    Only keyframes are decoded. Everything else is skipped, which is what makes reading an 8K
    film minutes rather than an afternoon - though the whole file still has to be read off the
    disk or the network, and that is the floor on how quick this can be.

    Only the left eye is looked at; the right shows the same scene.
    """
    import numpy as np

    detector = _detector()
    eye = max(2, (width // 2) // 2 * 2)
    # The middle of the eye, squared off: what the viewer is facing.
    side = max(2, int(eye * CENTRE) // 2 * 2)
    inset_x, inset_y = (eye - side) // 2, max(0, (height - side) // 2)
    # ffmpeg splits filter arguments on colons, so a Windows path inside one is parsed as an
    # option name and the whole graph is rejected. Escaping it is a thicket; running from the
    # folder and naming the file plainly avoids the question.
    work = tempfile.mkdtemp(prefix="blueshelf-scan-")
    times_path = os.path.join(work, "times.txt")
    step_path = os.path.join(work, "progress.txt")
    trouble_path = os.path.join(work, "ffmpeg.txt")
    chain = (f"crop={eye}:ih:0:0,crop={side}:{side}:{inset_x}:{inset_y},"
             f"scale={SAMPLE_W}:{SAMPLE_H},metadata=print:file=times.txt")
    trouble = open(trouble_path, "wb")
    process = subprocess.Popen(
        # -progress says how far into the video it has reached, and unlike the metadata
        # filter it flushes as it goes - it exists to be watched. That is what turns this
        # from a spinner into a percentage.
        ["ffmpeg", "-v", "error", "-progress", "progress.txt", "-skip_frame", "nokey",
         "-i", source, "-vf", chain, "-pix_fmt", "bgr24", "-vsync", "0",
         "-f", "rawvideo", "-"],
        cwd=work, stdout=subprocess.PIPE, stderr=trouble)

    each = SAMPLE_W * SAMPLE_H * 3
    values: list[float] = []
    exposure: list[float] = []
    furthest = [0.0]
    try:
        while True:
            block = process.stdout.read(each)
            if not block or len(block) < each:
                break
            frame = np.frombuffer(block, dtype=np.uint8).reshape(SAMPLE_H, SAMPLE_W, 3)
            values.append(_skin_share(frame))
            exposure.append(_exposure(detector, frame))
            if len(values) % 4 == 0:
                # Never let it slip backwards: the report is written by another process and
                # an early read can come back empty, and a bar that jumps back looks broken.
                so_far = max(furthest[0], _position(step_path))
                furthest[0] = so_far
                share = min(0.98, so_far / duration) if duration > 0 else -1.0
                say(event="progress", share=round(share, 4) if share >= 0 else -1.0,
                    frame=len(values), frames=0,
                    stage=f"Reading the video - {len(values)} samples, "
                          f"{_clock(so_far)} of {_clock(duration)}")
        process.stdout.close()
        process.wait(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
        trouble.close()

    times = _all_times(times_path)
    if not values:
        # Nothing came back at all. Say why rather than reporting an empty video.
        why = ""
        try:
            with open(trouble_path, "r", encoding="utf-8", errors="replace") as lines:
                why = lines.read().strip()[-600:]
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"Nothing could be read from the video.\n{why}")
    shutil.rmtree(work, ignore_errors=True)
    keep = min(len(times), len(values))
    if not keep:
        # No timestamps came back: fall back to spreading the samples evenly, which is close
        # enough for stretches measured in minutes.
        step = duration / max(1, len(values))
        times, keep = [n * step for n in range(len(values))], len(values)
    return {"times": times[:keep], "values": values[:keep],
            "exposure": exposure[:keep] if detector is not None else [],
            "detector": detector is not None}


_DETECTOR = []


def _detector():
    """The exposure detector, loaded once, or None if it is not installed."""
    if _DETECTOR:
        return _DETECTOR[0]
    try:
        from nudenet import NudeDetector
        _DETECTOR.append(NudeDetector())
    except Exception:  # noqa: BLE001 - not having it is ordinary, not an error
        _DETECTOR.append(None)
    return _DETECTOR[0]


def _skin_share(frame) -> float:
    """How much of the frame is skin-coloured, between 0 and 1."""
    import cv2
    import numpy as np

    ycc = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
    luma, cr, cb = ycc[:, :, 0], ycc[:, :, 1], ycc[:, :, 2]
    skin = ((cb >= SKIN_CB[0]) & (cb <= SKIN_CB[1])
            & (cr >= SKIN_CR[0]) & (cr <= SKIN_CR[1])
            & (luma >= SKIN_MIN_LUMA))
    return round(float(np.mean(skin)), 4)


def _exposure(detector, frame) -> float:
    """How sure the detector is that something explicit is on screen, between 0 and 1."""
    if detector is None:
        return 0.0
    try:
        found = detector.detect(frame)
    except Exception:  # noqa: BLE001 - one odd frame must not end the scan
        return 0.0
    scores = [f["score"] for f in found if f.get("class") in EXPOSED]
    return round(float(max(scores)), 4) if scores else 0.0


def _position(path: str) -> float:
    """How far into the video ffmpeg has reached, in seconds, from its progress report."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            back = min(handle.tell(), 4096)
            handle.seek(-back, os.SEEK_END)
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return 0.0
    for line in reversed(tail.splitlines()):
        name, _, value = line.partition("=")
        if name == "out_time_us" and value.strip().lstrip("-").isdigit():
            return max(0.0, int(value) / 1_000_000)
        if name == "out_time_ms" and value.strip().lstrip("-").isdigit():
            return max(0.0, int(value) / 1_000_000)   # ffmpeg writes microseconds here too
    return 0.0


def _clock(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _all_times(path: str) -> list[float]:
    """The pts_time of every frame ffmpeg wrote out."""
    found = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as lines:
            for line in lines:
                if "pts_time:" in line:
                    try:
                        found.append(float(line.split("pts_time:")[1].split()[0]))
                    except (IndexError, ValueError):
                        continue
    except OSError:
        return []
    return found


def look(spec: dict) -> dict:
    """Read a video through cheaply and report how much skin is on screen over time."""
    from vrautomatte.utils.ffmpeg import get_video_info

    source = spec["source"]
    info = get_video_info(source)
    fps = float(info["fps"]) or 30.0
    frames = int(info["num_frames"])
    duration = frames / fps if fps else 0.0
    say(event="progress", stage="Opening the video", share=0.02)
    read = read_video(source, int(info["width"]), int(info["height"]), duration)
    say(event="progress", stage=f"Read {len(read['values'])} samples", share=1.0)
    read.update({"fps": fps, "frames": frames})
    return read


# ── Making VRAutoMatte encode with something that exists ─────────────────────
#
# VRAutoMatte asks ffmpeg for libx264 or libx265 in a dozen places, falling back to them
# whenever it cannot see NVENC. Neither is in the ffmpeg BlueShelf ships: that build is LGPL
# with no --enable-gpl, on purpose, and x264 and x265 are both GPL. So the fallback fails with
# "Unknown encoder" after the matting is already done, which is the worst possible moment.
#
# Its own NVENC check is not enough to rely on either. It shells out to `ffmpeg -encoders`
# with a ten second limit and swallows any failure - and it runs that check just after a long
# matte, with the card and the disk still busy, where ten seconds is not a lot. When it times
# out, everything silently falls back to the encoder that is not there.
#
# And even when NVENC is found, H.264 through NVENC stops at 4096 pixels across. Every video
# this feature exists for is wider than that.
#
# So rather than editing VRAutoMatte - a checkout that gets updated - its encoder choice is
# replaced here, with one settled by encoding a real frame at the real size.

_ENCODERS = (
    ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", "0"],
    ["-c:v", "av1_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", "0"],
    ["-c:v", "libsvtav1", "-preset", "8"],
    ["-c:v", "libopenh264", "-b:v", "24M"],
    ["-c:v", "mpeg4", "-q:v", "3"],
)
_QUALITY = {"hevc_nvenc": "-cq", "av1_nvenc": "-cq", "libsvtav1": "-crf"}


def pick_encoder(width: int, height: int, crf: int = 20) -> list:
    """Arguments for an encoder proved to write this size on this machine."""
    for candidate in _ENCODERS:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
             "-i", f"color=c=white:s={width}x{height}:r=30", "-frames:v", "1",
             *candidate, "-pix_fmt", "yuv420p", "-f", "null", "-"],
            capture_output=True, timeout=600)
        if done.returncode == 0:
            flag = _QUALITY.get(candidate[1])
            return candidate + ([flag, str(crf)] if flag else [])
    raise RuntimeError(
        f"No encoder in this ffmpeg can write {width}x{height}. Tried: "
        + ", ".join(c[1] for c in _ENCODERS))


def keep_mask_small() -> None:
    """Stop VRAutoMatte blowing the finished mask up to the film's full size.

    It mattes at a fraction of the frame and then enlarges the result to 7K - its "Upscaling
    matte video" step - which BlueShelf then stored at 7K, joined at 7K, and shrank again for
    packing and for playing. The enlargement is turned into a plain copy here, so the mask
    leaves at the size it was worked at: nothing re-encoded, nothing lost.
    """
    import sys

    from vrautomatte.utils import ffmpeg as tools

    original = tools._run_ffmpeg_logged

    def run(args, tag, *rest, **named):
        if tag == "matte-upscale" and "-i" in args:
            source = args[args.index("-i") + 1]
            args = ["ffmpeg", "-y", "-i", source, "-c:v", "copy", "-an", args[-1]]
        return original(args, tag, *rest, **named)

    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("vrautomatte") and \
                getattr(module, "_run_ffmpeg_logged", None) is original:
            module._run_ffmpeg_logged = run


def teach_encoders(width: int, height: int) -> str:
    """Point every encoder choice inside VRAutoMatte at one that works here."""
    import sys

    chosen = pick_encoder(width, height)
    name = chosen[1]

    def encode_args(codec, crf=20, preset=None):
        return pick_encoder(width, height, int(crf))

    def stream_args(crf=20):
        return pick_encoder(width, height, int(crf))

    # Replaced on every module that has already taken its own reference by name, not just on
    # the one that defines them - "from ... import stream_encode_args" copies the function.
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("vrautomatte"):
            continue
        for label, replacement in (("_encode_args", encode_args),
                                   ("_encode_args_cpu", encode_args),
                                   ("stream_encode_args", stream_args)):
            if hasattr(module, label):
                setattr(module, label, replacement)
    return name


# VRAutoMatte's ffmpeg wrapper already writes lines like
#   [fisheye] frame 186/1201 (15%) elapsed 5s
# to its log. Converting a 52-minute 8K film to fisheye and packing the mask into it takes
# many minutes, and none of it was reaching the window - so the one thing left to do looked
# like nothing happening at all.
FFMPEG_LINE = re.compile(r"\[([\w-]+)\] frame (\d+)/(\d+)")


def forward_ffmpeg_progress(first: float, last: float):
    """Pass VRAutoMatte's own ffmpeg progress on, mapped into a span of the whole job."""
    from loguru import logger

    def listen(message) -> None:
        found = FFMPEG_LINE.search(str(message))
        if not found:
            return
        label, at, total = found.group(1), int(found.group(2)), int(found.group(3))
        share = at / total if total else 0.0
        say(event="progress", share=round(first + (last - first) * min(1.0, share), 4),
            stage=_STAGE_NAMES.get(label, label.replace("-", " ")),
            frame=at, frames=total)

    return logger.add(listen, level="INFO", format="{message}")


_STAGE_NAMES = {
    "fisheye": "Converting to fisheye",
    "matte-upscale": "Bringing the matte up to full size",
    "pack-alpha": "Packing the cut-out into the video",
    "trim-source": "Trimming the source",
    "trim-fisheye": "Trimming the source",
}


def build_config(spec: dict, output: str, matte_only: bool):
    from vrautomatte.pipeline.runner import OutputFormat, PipelineConfig, ProjectionType

    config = PipelineConfig(
        input_path=spec["source"],
        output_path=output,
        model_variant=spec.get("model", "mobilenetv3"),
        output_format=OutputFormat.MATTE_ONLY if matte_only else OutputFormat.DEOVR_ALPHA,
        projection=(ProjectionType.FISHEYE if spec.get("projection") == "fisheye"
                    else ProjectionType.EQUIRECTANGULAR),
        fisheye_fov=int(spec.get("fov", 180)),
        is_sbs=bool(spec.get("sbs", True)),
        start_frame=int(spec.get("start", 0)),
        end_frame=int(spec.get("end", 0)),
        crf=int(spec.get("crf", 18)),
        matte_stride=int(spec.get("stride", 1)),
        roi_matting=bool(spec.get("roi", True)),
        temporal_smoothing=float(spec.get("smoothing", 1.0)),
    )
    # How much of the frame the model sees. Left alone, VRAutoMatte picks this from the
    # card's memory and is far too cautious about it - see DETAIL in passthrough.py.
    if spec.get("max_matting_pixels"):
        config.max_matting_pixels = int(spec["max_matting_pixels"])
    if spec.get("downsample_ratio"):
        config.downsample_ratio = float(spec["downsample_ratio"])
    if spec.get("temp_dir"):
        config.temp_dir = spec["temp_dir"]
    config.auto_resume = bool(spec.get("resume", True))
    if config.projection == ProjectionType.EQUIRECTANGULAR and not matte_only:
        from vrautomatte.utils.masks import ensure_mask
        config.fisheye_mask_path = str(ensure_mask())
    return config


def run_pipeline(spec: dict, output: str, matte_only: bool) -> str:
    from vrautomatte.pipeline.runner import Pipeline
    from vrautomatte.utils.ffmpeg import get_video_info

    info = get_video_info(spec["source"])
    say(event="progress", share=0.0, stage="Choosing an encoder that works here",
        frame=0, frames=0)
    picked = teach_encoders(int(info["width"]), int(info["height"]))
    say(event="progress", share=0.0, stage=f"Encoding with {picked}", frame=0, frames=0)
    if spec.get("small_mask"):
        keep_mask_small()

    config = build_config(spec, output, matte_only)
    seen = {"share": -1.0, "stage": None}

    def on_progress(p) -> None:
        # The callback also carries preview images; those are dropped - only numbers travel.
        share = 0.0
        if p.total_frames:
            share = min(1.0, p.frame_num / p.total_frames)
        if p.total_stages:
            share = (max(0, p.stage_num - 1) + share) / p.total_stages
        # Chatter costs more than it tells: report on a real change only.
        if share - seen["share"] < 0.002 and p.stage == seen["stage"]:
            return
        seen["share"], seen["stage"] = share, p.stage
        say(event="progress", stage=p.stage, share=round(share, 4),
            frame=p.frame_num, frames=p.total_frames,
            fps=round(p.fps, 2), eta=round(p.eta_sec), elapsed=round(p.elapsed_sec))

    return str(Pipeline(config, on_progress).run())


def frames_in(path) -> int:
    """How many frames a file holds, or zero if it cannot be read."""
    from vrautomatte.utils.ffmpeg import get_video_info
    try:
        return int(get_video_info(str(path)).get("num_frames") or 0)
    except Exception:  # noqa: BLE001 - an unreadable file simply is not finished
        return 0


def already_made(path, wanted: int) -> bool:
    """Whether a stage from an earlier attempt is there and the right length to trust."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 4096 or wanted <= 0:
        return False
    return frames_in(path) == wanted


def one_pass_graph(width: int, height: int, fov: int, equirect: bool,
                   small_matte: bool = False) -> str:
    """Project the picture and the mask, and pack one into the other, in a single graph.

    It used to be three passes over the whole film: the picture to fisheye, the mask to
    fisheye, then the two combined. Each wrote a file the size of the film and read it back,
    and on a library kept over a network that is most of the cost - measured at 85 minutes
    for the picture conversion alone. Doing it in one graph never writes them at all.

    Checked against the three-pass result on real frames: the pictures differ by 0.8 of 255,
    which is the encoder rounding differently, not a different picture.
    """
    from vrautomatte.utils.ffmpeg import _alpha_pack_graph_parts

    turn = (f"v360=hequirect:fisheye:iv_fov=180:ih_fov=180:v_fov={fov}:h_fov={fov}")
    # A small matte is projected straight to the size the packing wants - 40% of an eye - so
    # it is resampled once, on the way up, rather than twice. A full-size one keeps the path
    # it was checked against: projecting a 7K matte down 2.5x in one go would rough its edges.
    mat_turn = turn
    if small_matte:
        eye = ((width // 2) * 4 // 10) & ~1
        tall = (height * 4 // 10) & ~1
        mat_turn = f"{turn}:w={eye}:h={tall}:interp=lanczos"
    parts = []
    if equirect:
        parts += [
            "[0:v]split=2[pl][pr]",
            f"[pl]crop=iw/2:ih:0:0,{turn}[plf]",
            f"[pr]crop=iw/2:ih:iw/2:0,{turn}[prf]",
            "[plf][prf]hstack[pstk]",
            # the circular lens mask, so the corners are black and ready to carry the pieces
            "[2:v][pstk]scale2ref[lens][pbase]",
            "[pbase][lens]overlay=0:0:shortest=1[pic]",
            "[1:v]split=2[ml][mr]",
            f"[ml]crop=iw/2:ih:0:0,{mat_turn}[mlf]",
            f"[mr]crop=iw/2:ih:iw/2:0,{mat_turn}[mrf]",
            "[mlf][mrf]hstack[mat]",
        ]
    else:
        # Already fisheye: nothing to project, just pack.
        parts += ["[0:v]null[pic]", "[1:v]null[mat]"]
    parts += _alpha_pack_graph_parts(width, height, "[pic]", "[mat]")
    return ";".join(parts)


def finish(spec: dict) -> str:
    """Turn a picture and its mask into the finished alpha copy, in one pass.

    ``start`` and ``end`` (1-based, inclusive) do a frame range only, writing a piece that
    can be joined to others. Unlike matting, nothing here depends on the frame before it, so
    a range can be done by any machine in any order with no run-up and nothing lost.
    """
    from vrautomatte.utils.ffmpeg import _encode_args, get_video_info

    source, matte, output = Path(spec["source"]), Path(spec["matte"]), Path(spec["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    info = get_video_info(str(source))
    width, height = int(info["width"]), int(info["height"])
    fps = float(info["fps"]) or 30.0
    teach_encoders(width, height)

    first = int(spec.get("start") or 0)
    last = int(spec.get("end") or 0)
    length = (min(last, int(info.get("num_frames") or last)) - max(first, 1) + 1
              if first or last else int(info.get("num_frames") or 0))
    if already_made(output, length):
        say(event="progress", stage="That piece was already made", share=1.0)
        return str(output)

    equirect = spec.get("projection") != "fisheye"
    kept = get_video_info(str(matte))
    small = int(kept.get("width") or width) < width * 0.6
    graph = one_pass_graph(width, height, int(spec.get("fov", 180)), equirect, small)

    seek = []
    if first > 1:
        seek = ["-ss", f"{(first - 1) / fps:.6f}"]
    args = ["ffmpeg", "-y", *seek, "-i", str(source), *seek, "-i", str(matte)]
    if equirect:
        from vrautomatte.utils.masks import ensure_mask
        args += ["-loop", "1", "-i", str(ensure_mask())]
    # The sound is carried straight through from the picture. Without this the copy comes
    # out silent, which is not obvious until it is already in a headset.
    args += ["-filter_complex", graph, "-map", "[out]", "-map", "0:a?"]
    if length:
        args += ["-frames:v", str(length), "-t", f"{length / fps:.6f}"]
    args += [*_encode_args("libsvtav1", int(spec.get("crf", 18))), "-c:a", "copy",
             "-progress", "pipe:1", str(output)]

    watching = forward_ffmpeg_progress(0.02, 0.99)
    try:
        from vrautomatte.utils.ffmpeg import _run_ffmpeg_logged
        _run_ffmpeg_logged(args, "pack-alpha", total_frames=length)
    finally:
        from loguru import logger
        logger.remove(watching)
    return str(output)


def graphics_memory_used() -> float:
    """The most graphics memory this run actually asked for, in MB.

    Asked of torch rather than nvidia-smi: on a consumer card under Windows nvidia-smi
    reports the whole card and cannot break it down per process, so its figure includes
    the desktop and whatever else happens to be drawing. That made a smaller setting look
    like it needed more memory than a larger one.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.max_memory_reserved() / (1024 * 1024)
    except Exception:  # noqa: BLE001 - a missing figure is not worth failing a job over
        pass
    return 0.0


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    mode = spec.get("mode", "matte")
    try:
        if mode == "probe":
            say(event="done", **probe())
        elif mode == "look":
            say(event="done", **look(spec))
        elif mode == "finish":
            say(event="done", output=finish(spec))
        elif mode == "matte":
            made = run_pipeline(spec, spec["output"], matte_only=True)
            say(event="done", output=made, vram_mb=round(graphics_memory_used(), 1))
        elif mode == "whole":
            made = run_pipeline(spec, spec["output"], matte_only=False)
            say(event="done", output=made, vram_mb=round(graphics_memory_used(), 1))
        else:
            raise ValueError(f"unknown mode {mode!r}")
    except InterruptedError:
        say(event="error", message="Cancelled.")
        return 2
    except BaseException as exc:  # noqa: BLE001 - the reason has to reach the other process
        say(event="error", message=f"{type(exc).__name__}: {exc}",
            detail=traceback.format_exc()[-4000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
