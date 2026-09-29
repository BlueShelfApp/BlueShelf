"""Keeping the cut-out at the size it was worked at, instead of blowing it up to 7K.

VRAutoMatte mattes at a fraction of the frame and used to enlarge the result to the film's
full size - which BlueShelf then stored, joined and shrank again for packing and for playing.
Now the mask stays small, packing enlarges it as it projects, and a film can stop at the
cut-out alone, ready to watch the moment matting is joined.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from blueshelf import paths, passthrough
from blueshelf.passthrough import Job, Slice


def _clip(path: Path, w: int, h: int, frames: int = 30, colour: str = "white") -> Path:
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={colour}:s={w}x{h}:r=30", "-frames:v", str(frames),
                    "-c:v", "libopenh264", "-pix_fmt", "yuv420p", str(path)],
                   timeout=300)
    return path


def _job(tmp_path: Path, width=2400, height=1200, **more) -> Job:
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    return Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
               width=width, height=height, fps=30.0, frames=30, detail="coarse", **more)


def _codec(path: Path) -> str:
    said = paths.run_tool([paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=codec_name", "-of", "default=nk=1:nw=1",
                           str(path)], timeout=60)
    return (said.stdout or b"").decode().strip()


# --------------------------------------------------------------------------- the size

def test_the_mask_is_kept_at_the_size_the_model_works_at():
    """Per eye for side-by-side, as VRAutoMatte does it, so its chunks arrive this size."""
    assert passthrough.working_mask_size(7168, 3584, "fine") == (2876, 1438)
    assert passthrough.working_mask_size(5760, 2880, "fine") == (2880, 1440)
    assert passthrough.working_mask_size(1920, 1080, "fine") == (1920, 1080), "never enlarged"


def test_a_new_job_is_small_and_one_with_old_full_size_work_stays_full_size(tmp_path):
    """Old 7K chunks and new small ones cannot be joined, so work on disk decides."""
    fresh = _job(tmp_path)
    passthrough.settle_mask_size(fresh)
    assert fresh.small_mask and (fresh.mask_w, fresh.mask_h) == (1920, 960)

    started = _job(tmp_path)
    folder = passthrough.work_folder(started)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "part-000.mp4").write_bytes(b"old full-size chunk")
    passthrough.settle_mask_size(started)
    assert not started.small_mask, "chunks with no size recorded are from before - full size"

    (folder / "settings.json").write_text('{"mask_w": 1920, "mask_h": 960}', encoding="utf-8")
    passthrough.settle_mask_size(started)
    assert (started.mask_w, started.mask_h) == (1920, 960), "a recorded size is kept"


def test_a_job_filed_before_small_masks_finishes_as_it_started():
    """A queued job's record has no size, and must come back as full size."""
    old = Job(source="x.mp4")
    assert not old.small_mask and old.make == "deovr" and not old.keep_mask
    assert old.spec()["small_mask"] is False


# --------------------------------------------------------------------------- the chunks

def test_every_chunk_leaves_at_the_job_size(tmp_path):
    """They must all agree or they will not join, whatever the machine handed over."""
    raw = _clip(tmp_path / "raw.mp4", 640, 320, frames=40)
    piece = Slice(0, 1, 11, 40)                    # ten frames of warm-up to drop
    cut = passthrough.prepare_segment(raw, piece, tmp_path / "cut.mp4", size=(480, 240))
    assert passthrough._frame_size(cut) == (480, 240)
    assert passthrough.frame_count(cut) == 30
    assert _codec(cut) == "h264", "a cut-out is played by browsers, so H.264"


def test_old_jobs_keep_their_codec_choice(monkeypatch):
    """Preferring H.264 is decided per job, never by size - or a 4K job under way would
    switch codec halfway and its chunks would no longer join."""
    class Worked:
        returncode = 0
    monkeypatch.setattr(passthrough.paths, "run_tool", lambda *a, **k: Worked())
    monkeypatch.setattr(passthrough, "_MASK_ENCODERS", {})

    as_before = passthrough.mask_encoder(640, 320)
    for_players = passthrough.mask_encoder(640, 320, for_players=True)

    assert as_before == passthrough._MASK_CANDIDATES[0], "unchanged for everything already running"
    assert "264" in for_players[1], "H.264 first only when the cut-out is to be played"


def test_blank_stretches_are_the_small_size_too(tmp_path):
    job = _job(tmp_path)
    passthrough.settle_mask_size(job)
    blank = passthrough.blank_segment(job, 12, tmp_path / "blank.mp4")
    assert passthrough._frame_size(blank) == (job.mask_w, job.mask_h)
    assert _codec(blank) == "h264"


# --------------------------------------------------------------------------- the cut-out

def test_a_small_matte_becomes_the_cut_out_without_being_re_encoded(tmp_path):
    job = _job(tmp_path, model="resnet50")
    passthrough.settle_mask_size(job)
    folder = passthrough.work_folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    matte = _clip(folder / "matte.mp4", job.mask_w, job.mask_h)

    kept = passthrough.keep_cutout(job, matte)

    assert kept == folder / "mask-resnet50.mp4", "named after the model, so several can sit"
    assert passthrough.frame_count(kept) == 30
    assert not matte.exists(), "the joined matte has become the cut-out"


def test_a_full_size_matte_from_an_older_job_is_shrunk_by_counting_frames(tmp_path):
    job = _job(tmp_path, model="mobilenetv3")         # full size: nothing settled
    folder = passthrough.work_folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    matte = _clip(folder / "matte.mp4", 2400, 1200, frames=24)

    kept = passthrough.keep_cutout(job, matte)

    assert passthrough._frame_size(kept) == (1920, 960)
    assert passthrough.frame_count(kept) == 24, "every frame, and not one more"
    assert _codec(kept) == "h264"


def test_tidying_keeps_small_chunks_and_never_a_cut_out(tmp_path):
    small = _job(tmp_path)
    passthrough.settle_mask_size(small)
    folder = passthrough.work_folder(small)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("part-000.mp4", "blank-001.mp4", "matte.mp4", "packed-000.mp4",
                 "mask-resnet50.mp4", "part-002.raw.mp4"):
        (folder / name).write_bytes(b"x")

    passthrough.tidy_after(small, folder)
    left = sorted(p.name for p in folder.iterdir())
    assert left == ["blank-001.mp4", "mask-resnet50.mp4", "part-000.mp4"], left

    passthrough.tidy_after(Job(source=small.source), folder)     # an old full-size job
    assert sorted(p.name for p in folder.iterdir()) == ["mask-resnet50.mp4"]


# --------------------------------------------------------------------------- the worker

def test_the_worker_stops_vrautomatte_blowing_the_mask_up(monkeypatch):
    """Its enlarging step is turned into a plain copy, so nothing is re-encoded to 7K."""
    seen = []
    tools = types.ModuleType("vrautomatte.utils.ffmpeg")
    tools._run_ffmpeg_logged = lambda args, tag, *rest, **named: seen.append((tag, args))
    utils = types.ModuleType("vrautomatte.utils")
    utils.ffmpeg = tools
    monkeypatch.setitem(sys.modules, "vrautomatte", types.ModuleType("vrautomatte"))
    monkeypatch.setitem(sys.modules, "vrautomatte.utils", utils)
    monkeypatch.setitem(sys.modules, "vrautomatte.utils.ffmpeg", tools)
    sys.path.insert(0, str(Path(passthrough.__file__).parent))
    try:
        import importlib
        worker = importlib.import_module("blueshelf.matte_worker")
        worker.keep_mask_small()
    finally:
        sys.path.pop(0)

    tools._run_ffmpeg_logged(["ffmpeg", "-y", "-i", "small.mp4", "-vf",
                              "scale=7168:3584:flags=lanczos", "-c:v", "hevc_nvenc", "out.mp4"],
                             "matte-upscale", total_frames=10)
    tools._run_ffmpeg_logged(["ffmpeg", "-i", "a.mp4", "b.mp4"], "pack-alpha")

    tag, args = seen[0]
    assert "scale=7168:3584:flags=lanczos" not in args and "-c:v" in args
    assert args[args.index("-c:v") + 1] == "copy" and args[-1] == "out.mp4"
    assert seen[1] == ("pack-alpha", ["ffmpeg", "-i", "a.mp4", "b.mp4"]), "nothing else touched"


def test_packing_a_small_matte_projects_it_straight_to_the_packed_size(monkeypatch):
    """Resampled once, on the way up, to 40% of an eye - what the packing wants. A full-size
    matte keeps the path it was checked against, since projecting 7K down 2.5x in one step
    would rough its edges."""
    tools = types.ModuleType("vrautomatte.utils.ffmpeg")
    tools._alpha_pack_graph_parts = lambda w, h, pic, mat: [f"{pic}{mat}overlay[out]"]
    utils = types.ModuleType("vrautomatte.utils")
    utils.ffmpeg = tools
    monkeypatch.setitem(sys.modules, "vrautomatte", types.ModuleType("vrautomatte"))
    monkeypatch.setitem(sys.modules, "vrautomatte.utils", utils)
    monkeypatch.setitem(sys.modules, "vrautomatte.utils.ffmpeg", tools)
    from blueshelf import matte_worker

    small = matte_worker.one_pass_graph(7168, 3584, 180, True, small_matte=True)
    full = matte_worker.one_pass_graph(7168, 3584, 180, True, small_matte=False)

    matte_parts = [p for p in small.split(";") if p.startswith(("[ml]", "[mr]"))]
    assert matte_parts and all("w=1432:h=1432:interp=lanczos" in p for p in matte_parts)
    picture_parts = [p for p in small.split(";") if p.startswith(("[pl]", "[pr]"))]
    assert all("w=" not in p for p in picture_parts), "the picture is projected as before"
    assert "interp=lanczos" not in full, "a full-size matte takes the old, checked path"
