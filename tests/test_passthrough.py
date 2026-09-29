"""Cutting a video into shares, naming the result, and the folder two machines claim work in."""
from __future__ import annotations

import json
import os
import time
import tempfile
import shutil
import pathlib
from pathlib import Path

import pytest

from blueshelf import farm, passthrough, paths
from blueshelf.passthrough import Job, Slice


# --------------------------------------------------------------------------- naming

@pytest.mark.parametrize("source, expected", [
    ("Studio Scene 8K.mp4", "Studio Scene 8K_FISHEYE180_alpha.mp4"),
    # A lens tag already there is replaced, not stacked - two would leave DeoVR guessing.
    ("SLR_Scene_FISHEYE190.mp4", "SLR_Scene_FISHEYE180_alpha.mp4"),
    ("Thing_MKX200.mp4", "Thing_FISHEYE180_alpha.mp4"),
    # Redoing a copy of a copy gives the same name back rather than growing it.
    ("Thing_FISHEYE180_alpha.mp4", "Thing_FISHEYE180_alpha.mp4"),
])
def test_output_name(source, expected):
    assert passthrough.output_name(source, 180) == expected


def test_output_name_carries_the_lens_angle():
    assert passthrough.output_name("Scene.mp4", 190).endswith("_FISHEYE190_alpha.mp4")


@pytest.mark.parametrize("name, angle", [
    ("SLR_Scene_FISHEYE190_alpha.mp4", 190),
    ("Thing_MKX200.mp4", 200),
    ("Other_VRCA220.mp4", 220),
    # The other common habit, which sent a real 180 file through as 170 until it was read.
    ("LethalHardcoreVR_Kimora_4096p_original_LR_180.mp4", 180),
    ("Studio_4096p_TB_180.mp4", 180),
    ("Scene_180x180_LR.mp4", 180),
    ("Scene_190x190_LR.mp4", 190),
    ("Scene_180x90_LR.mp4", None),           # a pair that disagrees says nothing
    ("VirtualTaboo_VR180_3630p.mp4", None),   # 3630p is a height, not a lens
    ("plain.mp4", None),
])
def test_the_lens_angle_is_read_from_the_name(name, angle):
    """A copy made from a 190 file and named 180 plays back too narrow in the headset."""
    assert passthrough.lens_angle(name) == angle


def test_fisheye_is_recognised_from_the_name():
    assert passthrough.guess_projection(7680, 3840, "SLR_x_FISHEYE190_alpha.mp4") == "fisheye"
    assert passthrough.guess_projection(7680, 3840, "VirtualTaboo_VR180_3630p.mp4") \
        == "equirectangular"


# --------------------------------------------------------------------------- splitting

def test_one_machine_takes_the_whole_thing():
    pieces = passthrough.split(1000, 1)
    assert pieces == [Slice(0, 1, 1, 1000)]
    assert pieces[0].lead_in == 0


def test_shares_cover_every_frame_exactly_once():
    for parts in (2, 3, 4, 7):
        pieces = passthrough.split(9999, parts, lead_in=60)
        assert pieces[0].keep_from == 1
        assert pieces[-1].end == 9999
        for before, after in zip(pieces, pieces[1:]):
            assert after.keep_from == before.end + 1, "a gap or an overlap in what is kept"
        assert sum(p.kept for p in pieces) == 9999


def test_every_share_but_the_first_mattes_a_run_up():
    """The model carries state between frames, so a share started cold gets its first
    moments wrong. It mattes ahead of its share and throws that part away."""
    pieces = passthrough.split(6000, 3, lead_in=60)
    assert pieces[0].lead_in == 0, "the first share starts where the video does"
    for piece in pieces[1:]:
        assert piece.lead_in == 60
        assert piece.start == piece.keep_from - 60


def test_a_run_up_never_reaches_before_the_first_frame():
    pieces = passthrough.split(100, 4, lead_in=500)
    assert all(p.start >= 1 for p in pieces)


def test_more_shares_than_frames_is_not_asked_for():
    assert len(passthrough.split(3, 8)) == 3


def test_an_empty_video_is_refused():
    with pytest.raises(ValueError):
        passthrough.split(0, 2)


# --------------------------------------------------------------------------- the job

def test_a_job_hands_the_worker_only_what_it_understands():
    job = Job(source="a.mp4", output="b.mp4", frames=1000, fps=30.0, width=7680, parts=4)
    spec = job.spec(mode="matte", start=5, end=99)
    assert spec["mode"] == "matte" and spec["start"] == 5 and spec["end"] == 99
    for local in ("frames", "fps", "width", "height", "parts", "extras"):
        assert local not in spec, f"{local} is BlueShelf's own bookkeeping"


def test_the_estimate_falls_as_machines_are_added():
    job = Job(source="a.mp4", frames=150_000, fps=60.0, parts=1)
    alone = passthrough.estimate_minutes(job)
    job.parts = 2
    assert 0 < passthrough.estimate_minutes(job) < alone


# --------------------------------------------------------------------------- the shared folder

def a_job_at(tmp_path, frames=6000, fps=30.0):
    """A job whose output has somewhere real to live - the farm keeps a job's chunks in the
    working folder beside the video, so a made-up drive letter will not do."""
    source = tmp_path / "scene.mp4"
    source.write_bytes(b"x")
    return Job(source=str(source), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
               width=320, height=180, fps=fps, frames=frames)


@pytest.fixture()
def share(tmp_path):
    made = farm.Share(tmp_path / "farm", machine="first")
    assert made.usable() == ""
    return made


def a_job(tmp_path) -> tuple[Job, list[Slice]]:
    return a_job_at(tmp_path, frames=1200), passthrough.split(1200, 3, lead_in=30)


def test_a_posted_job_comes_back_as_it_went_in(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    again, back = share.read_job(job_id)
    assert again == job
    assert back == pieces
    assert share.jobs() == [job_id]


def test_two_machines_never_get_the_same_share(share, tmp_path):
    job, pieces = a_job(tmp_path)
    share.post(job, pieces)
    other = farm.Share(tmp_path / "farm", machine="second")
    taken = []
    while True:
        got = share.claim() if len(taken) % 2 == 0 else other.claim()
        if got is None:
            break
        taken.append(got[1].index)
    assert sorted(taken) == [0, 1, 2], "every share taken"
    assert len(set(taken)) == 3, "and none of them twice"


def test_a_claimed_share_is_no_longer_on_offer(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    share.claim()
    state = share.state(job_id)
    assert state["running"] == 1 and state["waiting"] == 2
    assert state["machines"] == ["first"]


def test_finishing_a_share_files_the_segment(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    _id, piece, _job = share.claim()
    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"not really a video")
    share.finish(job_id, piece.index, segment)
    assert share.segment_of(job_id, piece).is_file()
    assert not segment.exists(), "it is moved, not copied - the staging copy is large"
    assert share.state(job_id)["done"] == 1


def test_a_job_is_only_complete_when_every_share_is(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    for n in range(3):
        _id, piece, _job = share.claim()
        part = tmp_path / f"{n}.mp4"
        part.write_bytes(b"x")
        assert not share.state(job_id)["complete"]
        share.finish(job_id, piece.index, part)
    assert share.state(job_id)["complete"]
    assert len(share.segments(job_id)) == 3


def test_segments_are_withheld_until_they_are_all_there(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    _id, piece, _job = share.claim()
    part = tmp_path / "one.mp4"
    part.write_bytes(b"x")
    share.finish(job_id, piece.index, part)
    assert share.segments(job_id) == [], "joining half a video would be worse than waiting"


def test_a_machine_that_goes_quiet_loses_its_share(share, tmp_path):
    """A laptop that is closed mid-share must not take the work with it."""
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    _id, piece, _job = share.claim()
    stale = share.running_file(job_id, piece.index)
    os.utime(stale, (0, 0))
    assert share.recover(stale_after=60) == 1
    assert share.state(job_id)["waiting"] == 3


def test_a_machine_still_working_keeps_its_share(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    _id, piece, _job = share.claim()
    os.utime(share.running_file(job_id, piece.index), (0, 0))
    share.beat(job_id, piece.index)
    assert share.recover(stale_after=60) == 0


def test_leaving_politely_puts_the_share_back(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    _id, piece, _job = share.claim()
    share.release(job_id, piece.index)
    assert share.state(job_id)["waiting"] == 3


def test_a_failure_is_recorded_with_its_reason(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    _id, piece, _job = share.claim()
    share.fail(job_id, piece.index, "the card ran out of memory")
    state = share.state(job_id)
    assert state["failed"] == 1
    assert "ran out of memory" in state["failures"][0]
    assert share.claim(job_id) is not None, "the other shares are still there to take"


def test_a_failed_share_can_be_offered_again(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    _id, piece, _job = share.claim()
    share.fail(job_id, piece.index, "a fluke")
    share.retry(piece.index and job_id or job_id, piece.index)
    assert share.state(job_id)["waiting"] == 3


def test_an_empty_queue_offers_nothing(share, tmp_path):
    assert share.claim() is None


def test_forgetting_a_job_clears_it_away(share, tmp_path):
    job, pieces = a_job(tmp_path)
    job_id = share.post(job, pieces)
    share.forget(job_id)
    assert share.jobs() == []


def test_a_machine_name_survives_being_put_in_a_file_name(share, tmp_path):
    job, pieces = a_job(tmp_path)
    awkward = farm.Share(share.root, machine="me@host with spaces/and\\slashes")
    job_id = awkward.post(job, pieces)
    got = awkward.claim()
    assert got is not None
    assert awkward.state(job_id)["running"] == 1


def test_an_unreachable_folder_says_so(tmp_path):
    blocked = farm.Share(tmp_path / "nope" / "\0bad")
    assert blocked.usable() != ""


# --------------------------------------------------------------------------- measuring

def test_the_library_answers_instead_of_ffprobe():
    """Choosing a performer with fifty scenes must not mean fifty probes before the window
    can draw an estimate."""
    row = {"width": 7680, "height": 3840, "fps": 59.94, "duration": 600.0}
    facts = passthrough.facts_from_library(row)
    assert facts == {"width": 7680, "height": 3840, "fps": 59.94,
                     "frames": round(600.0 * 59.94)}


@pytest.mark.parametrize("row", [
    {"width": 0, "height": 3840, "fps": 60.0, "duration": 600.0},
    {"width": 7680, "height": 3840, "fps": 0, "duration": 600.0},
    {"width": 7680, "height": 3840, "fps": 60.0, "duration": 0},
    {"width": None, "height": None, "fps": None, "duration": None},
    {},
])
def test_an_unprobed_video_falls_back_to_measuring(row):
    assert passthrough.facts_from_library(row) is None


def test_inspecting_with_known_numbers_runs_nothing(monkeypatch):
    monkeypatch.setattr(passthrough, "measure",
                        lambda _s: pytest.fail("it should not have probed"))
    job = passthrough.inspect(
        Job(source=r"X:\a\SLR_Scene_FISHEYE190.mp4"),
        {"width": 8000, "height": 4000, "fps": 60.0, "frames": 36000})
    assert job.frames == 36000 and job.projection == "fisheye" and job.fov == 190
    assert job.output.endswith("SLR_Scene_FISHEYE190_alpha.mp4")


def test_a_locked_lens_angle_beats_the_file_name():
    job = Job(source=r"X:\a\Scene_FISHEYE190.mp4", fov=200)
    job.extras["fov_locked"] = True
    passthrough.inspect(job, {"width": 8000, "height": 4000, "fps": 60.0, "frames": 100})
    assert job.fov == 200 and job.output.endswith("_FISHEYE200_alpha.mp4")


# --------------------------------------------------------------------------- finding the action

from blueshelf.passthrough import Stretch  # noqa: E402


def reading(pattern: str, fps: float = 30.0, every: float = 5.0):
    """Build a skin reading from a sketch: '.' is quiet, '#' is action, one mark per sample."""
    times = [n * every for n in range(len(pattern))]
    values = [0.30 if mark == "#" else 0.02 for mark in pattern]
    frames = int(round(len(pattern) * every * fps))
    return times, values, fps, frames


def test_a_quiet_video_has_nothing_to_matte():
    times, values, fps, frames = reading("." * 40)
    assert passthrough.stretches_from(times, values, fps, frames) == []


def test_the_talking_at_the_front_is_left_out():
    """The whole point: a scene that opens with five minutes of clothes should not be matted
    from the first frame."""
    times, values, fps, frames = reading("." * 60 + "#" * 60)
    found = passthrough.stretches_from(times, values, fps, frames, pad=0, shortest=10)
    assert len(found) == 1
    assert found[0].start > 1
    assert found[0].end == frames
    # Roughly the back half, give or take the sample spacing.
    assert 0.45 < found[0].length / frames < 0.55


def test_padding_widens_a_stretch_at_both_ends():
    times, values, fps, frames = reading("." * 20 + "#" * 20 + "." * 20)
    tight = passthrough.stretches_from(times, values, fps, frames, pad=0, shortest=10)[0]
    padded = passthrough.stretches_from(times, values, fps, frames, pad=10, shortest=10)[0]
    assert padded.start < tight.start and padded.end > tight.end


def test_a_short_lull_does_not_split_one_scene():
    times, values, fps, frames = reading("#" * 20 + "." * 2 + "#" * 20)
    assert len(passthrough.stretches_from(times, values, fps, frames, pad=0, bridge=45)) == 1


def test_a_long_gap_does_split_it():
    times, values, fps, frames = reading("#" * 20 + "." * 40 + "#" * 20)
    assert len(passthrough.stretches_from(times, values, fps, frames, pad=0, bridge=45)) == 2


def test_a_single_stray_sample_is_noise():
    times, values, fps, frames = reading("." * 30 + "#" + "." * 30)
    assert passthrough.stretches_from(times, values, fps, frames, pad=0, shortest=60) == []


def test_a_lower_threshold_matte_more():
    times = [n * 5.0 for n in range(60)]
    values = [0.08] * 60          # middling everywhere
    keen = passthrough.stretches_from(times, values, 30.0, 9000, threshold=0.05, shortest=5)
    fussy = passthrough.stretches_from(times, values, 30.0, 9000, threshold=0.20, shortest=5)
    assert sum(s.length for s in keen) > sum(s.length for s in fussy) == 0


def test_stretches_never_run_past_the_video():
    times, values, fps, frames = reading("#" * 40)
    for stretch in passthrough.stretches_from(times, values, fps, frames, pad=120):
        assert 1 <= stretch.start <= stretch.end <= frames


# --------------------------------------------------------------------------- the plan

def test_with_no_stretches_the_plan_is_just_the_split():
    assert passthrough.plan_slices(9000, None, 2, 60) == passthrough.split(9000, 2, 60)


def test_the_plan_covers_every_frame_exactly_once():
    pieces = passthrough.plan_slices(10000, [Stretch(3000, 5000), Stretch(7000, 8000)])
    ordered = sorted(pieces, key=lambda p: p.keep_from)
    assert ordered[0].keep_from == 1 and ordered[-1].end == 10000
    for before, after in zip(ordered, ordered[1:]):
        assert after.keep_from == before.end + 1
    assert sum(p.kept for p in pieces) == 10000


def test_the_gaps_are_blank_and_the_action_is_not():
    pieces = passthrough.plan_slices(10000, [Stretch(3000, 5000)])
    blanks = [p for p in pieces if p.blank]
    matted = [p for p in pieces if not p.blank]
    assert sum(p.kept for p in matted) == 2001
    assert sum(p.kept for p in blanks) == 10000 - 2001


def test_a_matted_stretch_still_gets_its_run_up():
    """Restarting in the middle of a scene needs the same warm-up as a split share does."""
    piece = next(p for p in passthrough.plan_slices(10000, [Stretch(3000, 5000)], lead_in=60)
                 if not p.blank)
    assert piece.keep_from == 3000 and piece.start == 2940 and piece.lead_in == 60


def test_a_stretch_from_the_first_frame_needs_no_run_up():
    piece = next(p for p in passthrough.plan_slices(10000, [Stretch(1, 5000)], lead_in=60)
                 if not p.blank)
    assert piece.start == 1 and piece.lead_in == 0


def test_overlapping_stretches_are_merged():
    pieces = passthrough.plan_slices(10000, [Stretch(3000, 5000), Stretch(4000, 6000)])
    matted = [p for p in pieces if not p.blank]
    assert len(matted) == 1 and matted[0].keep_from == 3000 and matted[0].end == 6000


def test_a_stretch_is_shared_out_when_several_machines_help():
    pieces = passthrough.plan_slices(10000, [Stretch(2001, 8000)], parts=3, lead_in=60)
    matted = [p for p in pieces if not p.blank]
    assert len(matted) == 3
    assert sum(p.kept for p in matted) == 6000
    for piece in matted:
        assert piece.lead_in == 60, "each share starts cold, so each needs a run-up"


def test_pieces_are_numbered_in_order():
    pieces = passthrough.plan_slices(10000, [Stretch(3000, 5000), Stretch(7000, 8000)])
    assert [p.index for p in pieces] == list(range(len(pieces)))


def test_a_blank_share_is_never_offered_to_another_machine(tmp_path):
    """Nobody should be asked to matte a stretch that was deliberately skipped."""
    place = farm.Share(tmp_path / "farm", machine="first")
    job = a_job_at(tmp_path, frames=10000)
    pieces = passthrough.plan_slices(10000, [Stretch(3000, 5000)])
    job_id = place.post(job, pieces)
    state = place.state(job_id)
    assert state["total"] == len([p for p in pieces if not p.blank])
    taken = []
    while (got := place.claim()) is not None:
        taken.append(got[1])
        place.finish(job_id, got[1].index, _a_file(tmp_path, got[1].index))
    assert all(not piece.blank for piece in taken)
    assert place.state(job_id)["complete"]


def _a_file(tmp_path, index):
    path = tmp_path / f"seg{index}.mp4"
    path.write_bytes(b"x")
    return path


def test_the_threshold_comes_from_the_videos_own_spread():
    """A fixed line is no use: a POV scene runs high all the way through."""
    quiet_then_loud = [0.02] * 40 + [0.30] * 40
    chosen = passthrough.suggest_threshold(quiet_then_loud)
    assert 0.02 < chosen < 0.30, chosen

    # The same shape lifted bodily upwards, as a close-up VR scene is, moves the line with it.
    all_high = [0.18] * 40 + [0.45] * 40
    assert passthrough.suggest_threshold(all_high) > chosen


def test_too_few_samples_fall_back_to_something_sane():
    assert 0 < passthrough.suggest_threshold([0.2, 0.3]) < 1


def test_a_flat_video_still_gives_a_usable_line():
    chosen = passthrough.suggest_threshold([0.2] * 60)
    assert 0.0 < chosen <= 0.45


# --------------------------------------------------------------------------- writing the mask

@pytest.mark.parametrize("size", [(1920, 1080), (7200, 3600)])
def test_an_encoder_is_found_for_the_frame_size(size):
    """Being listed is not the same as working, and working small is not working big:
    NVENC's H.264 stops at 4096 across, which is under every size this feature deals in."""
    args = passthrough.mask_encoder(*size)
    assert args[0] == "-c:v"
    assert "libx264" not in args and "libx265" not in args, \
        "both are GPL and are deliberately absent from the bundled ffmpeg"


def test_a_blank_stretch_is_exactly_as_long_as_it_was_asked_for(tmp_path):
    """A blank that comes out the wrong length slides the mask out of step with the picture
    for the whole rest of the video."""
    job = Job(source="x.mp4", output=str(tmp_path / "out.mp4"),
              width=640, height=360, fps=30.0)
    for wanted in (7, 45, 130):
        made = passthrough.blank_segment(job, wanted, tmp_path / f"blank-{wanted}.mp4")
        assert passthrough.frame_count(made) == wanted


def test_blank_stretches_join_into_one_run(tmp_path):
    job = Job(source="x.mp4", output=str(tmp_path / "out.mp4"),
              width=640, height=360, fps=30.0)
    pieces = [passthrough.blank_segment(job, n, tmp_path / f"p{n}.mp4") for n in (20, 35, 15)]
    joined = passthrough.join(pieces, tmp_path / "joined.mp4")
    assert passthrough.frame_count(joined) == 70


# --------------------------------------------------------------------------- judging by exposure

def test_exposure_beats_skin_on_a_real_scene():
    """The reading that started this: the skin measure called the last four minutes quiet,
    and the person watching said it was not. The detector agrees with the person."""
    import json
    from pathlib import Path as _Path
    saved = _Path(__file__).parent.parent / "passthrough-tests" / "reading2.json"
    if not saved.is_file():
        pytest.skip("no saved reading to check against")
    r = json.loads(saved.read_text())
    late = [v for t, v in zip(r["times"], r["exposure"]) if t > 480]
    assert max(late) > 0.5, "the detector should see the late action the skin measure missed"

    chosen = passthrough.suggest_threshold(r["exposure"], 0.15, 0.70)
    found = passthrough.stretches_from(r["times"], r["exposure"], r["fps"], r["frames"],
                                       threshold=chosen)
    assert len(found) == 1, "one run, not a scene broken in half by a false lull"
    assert sum(s.length for s in found) / r["frames"] > 0.85


def test_the_line_is_drawn_differently_for_each_measure():
    """A detector's confidence and a fraction of the frame are not the same kind of number."""
    scores = [0.0] * 30 + [0.8] * 30
    assert 0.15 <= passthrough.suggest_threshold(scores, 0.15, 0.70) <= 0.70
    fractions = [0.02] * 30 + [0.25] * 30
    assert 0.04 <= passthrough.suggest_threshold(fractions, 0.04, 0.45) <= 0.45


# --------------------------------------------------------------------------- working in chunks

def test_a_long_video_is_cut_into_chunks():
    """Matting forty minutes in one go means anything going wrong at the end throws away
    the lot."""
    pieces = passthrough.plan_slices(162000, None, chunk=5000)
    assert len(pieces) == 33
    assert max(p.kept for p in pieces) <= 5000
    assert sum(p.kept for p in pieces) == 162000


def test_chunking_and_skipping_work_together():
    pieces = passthrough.plan_slices(162000, [Stretch(10800, 144000)], chunk=5000)
    matted = [p for p in pieces if not p.blank]
    assert sum(p.kept for p in matted) == 144000 - 10800 + 1
    assert max(p.kept for p in matted) <= 5000
    assert sum(p.kept for p in pieces) == 162000
    assert all(p.lead_in > 0 for p in matted), "each chunk restarts cold and needs a run-up"


def test_no_chunking_leaves_one_piece():
    assert len(passthrough.plan_slices(9000, None, chunk=0)) == 1


def test_a_chunk_size_over_the_length_is_one_piece():
    assert len(passthrough.plan_slices(900, None, chunk=5000)) == 1


def test_a_chunk_size_beats_a_number_of_machines():
    """The chunk size says how much work may be lost, which is the stronger claim."""
    pieces = passthrough.plan_slices(60000, None, parts=2, chunk=5000)
    assert len(pieces) == 12
    assert max(p.kept for p in pieces) <= 5000


def test_a_finished_chunk_is_recognised(tmp_path):
    job = Job(source="x.mp4", output=str(tmp_path / "o.mp4"), width=320, height=180, fps=30.0)
    made = passthrough.blank_segment(job, 40, tmp_path / "part-000.mp4")
    assert passthrough.finished(made, 40)
    assert not passthrough.finished(made, 41), "the wrong length is not a finished chunk"
    assert not passthrough.finished(tmp_path / "nothing.mp4", 40)
    (tmp_path / "stub.mp4").write_bytes(b"x" * 8)
    assert not passthrough.finished(tmp_path / "stub.mp4", 40), "a stub is not a chunk"


# --------------------------------------------------------------------------- detail

def test_every_detail_level_names_a_budget_and_a_ratio():
    from blueshelf.passthrough import DETAIL, DETAIL_WHY
    assert set(DETAIL) == set(DETAIL_WHY)
    budgets = [DETAIL[n][0] for n in ("coarse", "normal", "fine", "finest")]
    assert budgets == sorted(budgets), "the levels should rise"


def test_the_detail_level_reaches_the_worker_as_numbers():
    """The worker gets pixels and a ratio; the word means nothing to VRAutoMatte."""
    from blueshelf.passthrough import DETAIL
    for name, (budget, ratio) in DETAIL.items():
        spec = Job(source="a.mp4", detail=name).spec(mode="matte")
        assert spec["max_matting_pixels"] == budget
        assert spec["downsample_ratio"] == ratio
        assert "detail" not in spec


def test_an_unknown_detail_level_falls_back_rather_than_failing():
    from blueshelf.passthrough import DETAIL
    spec = Job(source="a.mp4", detail="nonsense").spec(mode="matte")
    assert spec["max_matting_pixels"] == DETAIL["fine"][0]


def test_more_detail_is_estimated_as_slower():
    plain = Job(source="a.mp4", frames=100000, fps=60.0, detail="coarse")
    sharp = Job(source="a.mp4", frames=100000, fps=60.0, detail="finest")
    assert passthrough.estimate_minutes(sharp) > passthrough.estimate_minutes(plain)


# --------------------------------------------------------------------------- what it says

def test_a_finished_chunk_is_not_announced_as_a_finished_job(tmp_path, monkeypatch):
    """VRAutoMatte says "Complete" when one chunk ends. Passed through unchanged, a job of
    forty chunks declared itself done at the end of the first."""
    said = []
    job = Job(source="x.mp4", output=str(tmp_path / "out.mp4"),
              width=320, height=180, fps=30.0, frames=600)
    pieces = passthrough.plan_slices(600, None, chunk=200)
    assert len(pieces) == 3

    def fake_matte(root, job, piece, into, report, should_stop=None):
        report("Matting", 0.5, "")
        report("Complete", 1.0, "")          # what the real one emits at the end of a chunk
        passthrough.blank_segment(job, piece.end - piece.start + 1, into)
        return into

    monkeypatch.setattr(passthrough, "matte_slice", fake_matte)
    monkeypatch.setattr(passthrough, "pack_in_pieces",
                        lambda *a, **k: tmp_path / "out.mp4")
    notes = []
    passthrough.make_in_pieces("root", job, pieces,
                               lambda s, f, d: said.append((s, f)),
                               work_dir=tmp_path / "work", note=notes.append)

    stages = [s for s, _f in said]
    assert not any(s.strip().lower().startswith("complete") for s in stages), stages
    assert any("Chunk 1 of 3" in s for s in stages), "it should say which chunk"
    assert any("Chunk 3 of 3" in s for s in stages)
    assert any("tidying up" in s for s in stages), "the re-encode should not be silent"
    assert len(notes) == 3, "one line in the log per chunk"


def test_progress_climbs_across_the_chunks(tmp_path, monkeypatch):
    said = []
    job = Job(source="x.mp4", output=str(tmp_path / "out.mp4"),
              width=320, height=180, fps=30.0, frames=600)
    pieces = passthrough.plan_slices(600, None, chunk=200)

    def fake_matte(root, job, piece, into, report, should_stop=None):
        report("Matting", 1.0, "")
        passthrough.blank_segment(job, piece.end - piece.start + 1, into)
        return into

    monkeypatch.setattr(passthrough, "matte_slice", fake_matte)
    monkeypatch.setattr(passthrough, "pack_in_pieces",
                        lambda *a, **k: tmp_path / "out.mp4")
    passthrough.make_in_pieces("root", job, pieces,
                               lambda s, f, d: said.append(f), work_dir=tmp_path / "work")
    shares = [f for f in said if 0 <= f <= 1]
    assert shares == sorted(shares), "it should never go backwards"
    assert shares[-1] > 0.9


# --------------------------------------------------------------------------- who is working

def test_the_share_says_who_is_working_and_when_they_last_spoke(tmp_path):
    """From another room, the only question is whether that computer is still going."""
    place = farm.Share(tmp_path / "farm", machine="laptop")
    job = a_job_at(tmp_path, frames=6000)
    job_id = place.post(job, passthrough.split(6000, 1, 0, chunk=2000))
    assert place.workers(job_id) == []

    _id, piece, _job = place.claim()
    busy = place.workers(job_id)
    assert len(busy) == 1
    assert busy[0]["machine"] == "laptop"
    assert busy[0]["index"] == piece.index
    assert busy[0]["age"] < 60 and not busy[0]["stale"]


def test_a_machine_that_stopped_talking_is_flagged(tmp_path):
    place = farm.Share(tmp_path / "farm", machine="laptop")
    job = a_job_at(tmp_path, frames=6000)
    job_id = place.post(job, passthrough.split(6000, 1, 0, chunk=2000))
    _id, piece, _job = place.claim()
    os.utime(place.running_file(job_id, piece.index), (0, 0))
    assert place.workers(job_id)[0]["stale"], "hours of silence should show as trouble"


def test_two_machines_show_separately(tmp_path):
    first = farm.Share(tmp_path / "farm", machine="desktop")
    second = farm.Share(tmp_path / "farm", machine="laptop")
    job = a_job_at(tmp_path, frames=6000)
    job_id = first.post(job, passthrough.split(6000, 1, 0, chunk=2000))
    first.claim()
    second.claim()
    names = sorted(w["machine"] for w in first.workers(job_id))
    assert names == ["desktop", "laptop"]


# --------------------------------------------------------------------------- picking up again

def test_the_frame_count_comes_from_the_header(tmp_path, monkeypatch):
    """Counting every frame meant decoding the whole file - forty seconds a chunk on a local
    disk, minutes over a network, paid for every finished chunk before a restart did any work."""
    job = Job(source="x.mp4", output=str(tmp_path / "o.mp4"), width=320, height=180, fps=30.0)
    made = passthrough.blank_segment(job, 90, tmp_path / "part.mp4")
    monkeypatch.setattr(passthrough, "counted_frames",
                        lambda _p: pytest.fail("it should not have decoded the file"))
    assert passthrough.frame_count(made) == 90


def test_a_half_written_chunk_falls_back_to_counting(tmp_path):
    """A file a crash caught mid-write has no header to trust, and that is exactly the one
    whose length is in doubt."""
    job = Job(source="x.mp4", output=str(tmp_path / "o.mp4"), width=320, height=180, fps=30.0)
    made = passthrough.blank_segment(job, 90, tmp_path / "part.mp4")
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(made.read_bytes()[: int(made.stat().st_size * 0.4)])
    assert passthrough.frame_count(broken) != 90
    assert not passthrough.finished(broken, 90), "it must not pass as finished"


def test_restarting_reuses_what_was_finished(tmp_path, monkeypatch):
    """The whole point of chunks: a second attempt carries on rather than starting over."""
    job = Job(source="x.mp4", output=str(tmp_path / "out.mp4"),
              width=320, height=180, fps=30.0, frames=600)
    pieces = passthrough.plan_slices(600, None, chunk=200)
    work = tmp_path / "work"
    work.mkdir()
    # Two chunks survive from the run before.
    for piece in pieces[:2]:
        passthrough.blank_segment(job, piece.kept, work / f"part-{piece.index:03d}.mp4")

    matted = []

    def fake_matte(root, job, piece, into, report, should_stop=None):
        matted.append(piece.index)
        report("Matting", 1.0, "")
        passthrough.blank_segment(job, piece.end - piece.start + 1, into)
        return into

    monkeypatch.setattr(passthrough, "matte_slice", fake_matte)
    monkeypatch.setattr(passthrough, "pack_in_pieces",
                        lambda *a, **k: tmp_path / "out.mp4")
    notes = []
    passthrough.make_in_pieces("root", job, pieces, lambda *a: None,
                               work_dir=work, note=notes.append)
    assert matted == [2], "only the chunk that was never done"
    assert sum("already done" in n for n in notes) == 2


# --------------------------------------------------------------------------- handing work over

# Encoding the same handful of clips over and over was most of this file's running time:
# fifteen or so tests each spending a couple of seconds making something identical. They are
# made once and copied instead, which costs nothing and keeps every test its own folder.
_MADE_ONCE: dict[tuple, pathlib.Path] = {}


def _cached(key: tuple, build) -> pathlib.Path:
    """Build a file once per test session and hand back copies of it."""
    if key not in _MADE_ONCE:
        keep = pathlib.Path(tempfile.mkdtemp(prefix="blueshelf-fixture-"))
        _MADE_ONCE[key] = build(keep)
    return _MADE_ONCE[key]


def a_half_done_job(tmp_path, real_source=False):
    """Seven chunks matted alone, as a real folder looks when somebody stops to get help.

    ``real_source`` makes an actual video, for the tests that cut frames out of it.
    """
    video = tmp_path / "scene.mp4"
    if real_source:
        def build(keep):
            made = keep / "src.mp4"
            paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                            "testsrc=size=160x120:rate=30:duration=250", "-frames:v", "7400",
                            "-c:v", "libopenh264", "-b:v", "1M", "-y", str(made)], timeout=900)
            return made

        shutil.copy2(_cached(("source", 7400), build), video)
    else:
        video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=7400)
    pieces = passthrough.plan_slices(7400, None, chunk=1000)
    folder = passthrough.work_folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    for piece in pieces[:7]:

        def build(keep, piece=piece):
            return passthrough.blank_segment(job, piece.kept, keep / f"{piece.kept}.mp4")

        shutil.copy2(_cached(("chunk", piece.kept, job.width, job.height, job.fps), build),
                     passthrough.part_file(folder, piece))
    return job, pieces, folder


def test_chunks_matted_alone_are_taken_over(tmp_path):
    """Stopping on one machine and carrying on with two should not throw away the morning."""
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    assert place.ready_made(pieces, folder) == 7

    job_id = place.post(job, pieces)
    state = place.state(job_id)
    assert state["done"] == 7, "the finished ones should already count"
    assert state["waiting"] == len(pieces) - 7
    assert not state["complete"]


def test_only_the_rest_is_offered_to_other_machines(tmp_path):
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, pieces)
    taken = []
    while (got := place.claim()) is not None:
        taken.append(got[1].index)
        part = tmp_path / f"seg{got[1].index}.mp4"
        part.write_bytes(b"x")
        place.finish(job_id, got[1].index, part)
    assert all(index >= 7 for index in taken), "nobody should redo the first seven"
    assert place.state(job_id)["complete"]
    assert len(place.segments(job_id)) == len(pieces)


def test_a_half_written_chunk_is_not_taken_over(tmp_path):
    job, pieces, folder = a_half_done_job(tmp_path)
    ruined = passthrough.part_file(folder, pieces[3])
    ruined.write_bytes(ruined.read_bytes()[:400])
    place = farm.Share(tmp_path / "farm", machine="desktop")
    assert place.ready_made(pieces, folder) == 6
    job_id = place.post(job, pieces)
    assert place.state(job_id)["done"] == 6
    offered = {p.index for p in pieces if not p.blank} - set(range(7)) | {pieces[3].index}
    got = place.claim()
    assert got is not None and got[1].index in offered


def test_a_video_with_nothing_done_offers_everything(tmp_path):
    video = tmp_path / "fresh.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "fresh_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=7400)
    pieces = passthrough.plan_slices(7400, None, chunk=1000)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, pieces)
    assert place.state(job_id)["done"] == 0
    assert place.state(job_id)["waiting"] == len([p for p in pieces if not p.blank])


# --------------------------------------------------------------------------- across drives

def test_a_share_finishes_even_when_the_move_crosses_drives(tmp_path, monkeypatch):
    """Segments are built on local disk on purpose and the shared folder is on the network,
    so this move almost always crosses volumes - where a plain rename cannot go."""
    import errno

    place = farm.Share(tmp_path / "farm", machine="helper")
    job = a_job_at(tmp_path, frames=4000)
    job_id = place.post(job, passthrough.split(4000, 1, 0, chunk=1000))
    _id, piece, _job = place.claim()
    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"x" * 2048)

    def no_crossing(src, dst, *rest):
        raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")

    # What shutil.move tries first. The marker files beside it are renamed within one folder
    # and must keep working, so only this call is blocked.
    monkeypatch.setattr(os, "rename", no_crossing)
    place.finish(job_id, piece.index, staged)
    assert place.segment_of(job_id, piece).is_file(), "the segment must still be filed"
    assert not staged.exists(), "and the staging copy cleared away"


def test_failed_shares_can_be_offered_again(tmp_path):
    """A share usually fails for a reason shared by all of them; once it is dealt with the
    rest of the job is still good."""
    place = farm.Share(tmp_path / "farm", machine="helper")
    job = a_job_at(tmp_path, frames=4000)
    job_id = place.post(job, passthrough.split(4000, 1, 0, chunk=1000))
    for _ in range(2):
        _id, piece, _job = place.claim()
        place.fail(job_id, piece.index, "nowhere to write it")
    assert place.state(job_id)["failed"] == 2

    assert place.retry_failed(job_id) == 2
    state = place.state(job_id)
    assert state["failed"] == 0 and state["waiting"] == 4
    assert place.claim() is not None


# --------------------------------------------------------------------------- a look part-way

def test_a_partial_copy_never_overwrites_the_real_one(tmp_path):
    job, _pieces, _folder = a_half_done_job(tmp_path)
    partial = passthrough.partial_output(job)
    assert partial != job.output
    assert "sofar" in Path(partial).name
    assert Path(partial).name.endswith("_alpha.mp4"), "DeoVR still has to recognise it"
def test_chunks_live_in_one_place_whoever_made_them(tmp_path):
    """Working alone and sharing the work must write to the same folder, or switching
    between them means copying gigabytes about and risks stranding them."""
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, pieces)

    # The seven already matted are recognised where they lie - nothing moved.
    assert place.state(job_id)["done"] == 7
    assert place.ready_made(pieces, folder) == 7
    assert all(passthrough.part_file(folder, p).is_file() for p in pieces[:7])

    # And a helper's chunk lands in the same folder.
    _id, piece, _job = place.claim()
    staged = tmp_path / "staged.mp4"
    passthrough.blank_segment(job, piece.kept, staged)
    place.finish(job_id, piece.index, staged)
    assert passthrough.part_file(folder, piece).is_file()
    assert place.ready_made(pieces, folder) == 8


def test_forgetting_a_job_keeps_the_chunks(tmp_path):
    """The queue is throwaway; the chunks are hours of work and are not its to delete."""
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, pieces)
    place.forget(job_id)
    assert place.jobs() == []
    assert place.ready_made(pieces, folder) == 7, "the work must survive"


def test_a_machine_hands_back_what_it_left_holding(tmp_path):
    """A run that was killed rather than closed leaves its claim behind, and that chunk is
    then kept from every other machine until it falls silent for twenty minutes."""
    place = farm.Share(tmp_path / "farm", machine="laptop")
    job = a_job_at(tmp_path, frames=8000)
    job_id = place.post(job, passthrough.split(8000, 1, 0, chunk=1000))
    for _ in range(3):
        place.claim()
    assert place.state(job_id)["running"] == 3

    # The same machine starts again: it cannot be working on any of them.
    assert place.release_mine() == 3
    assert place.state(job_id)["running"] == 0
    assert place.state(job_id)["waiting"] == 8


def test_it_only_hands_back_its_own(tmp_path):
    place = farm.Share(tmp_path / "farm", machine="laptop")
    other = farm.Share(tmp_path / "farm", machine="desktop")
    job = a_job_at(tmp_path, frames=4000)
    job_id = place.post(job, passthrough.split(4000, 1, 0, chunk=1000))
    place.claim()
    other.claim()
    assert place.release_mine() == 1, "not the other machine's work"
    state = place.state(job_id)
    assert state["running"] == 1 and state["machines"] == ["desktop"]


def test_chunks_from_an_older_version_are_brought_back(tmp_path):
    """Chunks used to be written inside the job folder. They are hours of work each, so an
    upgrade must not quietly leave them behind."""
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, pieces)

    # Two more, filed the way the old version did it.
    old_out = place.job_dir(job_id) / "out"
    old_out.mkdir(parents=True, exist_ok=True)
    for piece in pieces[7:]:
        passthrough.blank_segment(job, piece.kept, old_out / f"{piece.index}.mp4")

    stranded = len(pieces) - 7
    assert place.ready_made(pieces, folder) == 7
    assert place.reclaim_old_layout(job, pieces) == stranded
    assert place.ready_made(pieces, folder) == 7 + stranded
    assert place.reclaim_old_layout(job, pieces) == 0, "and not again"


def test_filling_a_blank_stretch_reports_as_it_goes(tmp_path):
    """Each filler is minutes of 8K encoding. Doing it in silence is why the window looked
    stuck with nothing but a stretch number on it."""
    job = Job(source="x.mp4", output=str(tmp_path / "o.mp4"),
              width=640, height=360, fps=30.0, frames=600)
    seen = []
    passthrough.blank_segment(job, 300, tmp_path / "blank.mp4",
                              lambda share, at: seen.append((share, at)))
    assert seen, "it should say something while it works"
    assert seen[-1][1] > 0


def test_building_a_matte_names_the_stretch_and_moves(tmp_path, monkeypatch):
    job = Job(source="x.mp4", output=str(tmp_path / "o.mp4"),
              width=320, height=180, fps=30.0, frames=900)
    pieces = passthrough.plan_slices(900, [Stretch(400, 500)], chunk=300)
    work = tmp_path / "work"
    work.mkdir()
    for piece in pieces:
        if not piece.blank:
            passthrough.blank_segment(job, piece.kept, passthrough.part_file(work, piece))

    said = []
    passthrough.build_matte(job, pieces, lambda p: passthrough.part_file(work, p), work,
                            lambda s, f, d: said.append((s, f, d)))
    filling = [s for s, _f, _d in said if "Filling in" in s]
    assert filling, said
    assert any("frame" in d for _s, _f, d in said), "with a position, not just a count"
    shares = [f for _s, f, _d in said]
    assert shares == sorted(shares), "and never going backwards"


def test_a_preview_holds_only_the_parts_that_have_a_cut_out(tmp_path):
    """Filling the gaps would mean encoding hours of 8K to hand back the original video.
    The point of looking part-way through is to see the cut-out."""
    job, pieces, folder = a_half_done_job(tmp_path, real_source=True)
    done = [p for p in pieces if passthrough.finished(passthrough.part_file(folder, p), p.kept)]
    assert len(done) == 7

    seen = {}

    def fake_pack(_root, _job, source, matte, output, _report, _work, chunk=0):
        seen["matte"] = passthrough.frame_count(matte)
        seen["picture"] = passthrough.frame_count(source)
        return pathlib.Path(output)

    import blueshelf.passthrough as pt
    real, pt.pack_in_pieces = pt.pack_in_pieces, fake_pack
    try:
        pt.make_so_far("root", job, pieces, lambda *a: None)
    finally:
        pt.pack_in_pieces = real

    want = sum(p.kept for p in done)
    assert seen["matte"] == want, "the mask covers only the finished chunks"
    assert seen["picture"] == want, "and the picture is cut to match"
    assert want < job.frames, "it is shorter than the film, not the same length"


def test_a_preview_stays_in_step_across_a_missing_chunk(tmp_path):
    """Chunk 12 of a real run failed. The picture and the mask must still line up."""
    job, pieces, folder = a_half_done_job(tmp_path, real_source=True)
    passthrough.part_file(folder, pieces[3]).unlink()      # a gap in the middle
    done = [p for p in pieces if passthrough.finished(passthrough.part_file(folder, p), p.kept)]
    assert len(done) == 6

    seen = {}
    import blueshelf.passthrough as pt
    real = pt.pack_in_pieces
    pt.pack_in_pieces = lambda _r, _j, source, matte, output, _rep, _w, chunk=0: (
        seen.update(matte=pt.frame_count(matte), picture=pt.frame_count(source)),
        pathlib.Path(output))[1]
    try:
        pt.make_so_far("root", job, pieces, lambda *a: None)
    finally:
        pt.pack_in_pieces = real
    assert seen["matte"] == seen["picture"] == sum(p.kept for p in done)


def test_a_preview_leaves_the_real_work_alone(tmp_path):
    job, pieces, folder = a_half_done_job(tmp_path, real_source=True)
    before = sorted(f.name for f in folder.glob("part-*.mp4"))
    import blueshelf.passthrough as pt
    real = pt.pack_in_pieces
    pt.pack_in_pieces = lambda _r, _j, _s, _m, output, _rep, _w, chunk=0: pathlib.Path(output)
    try:
        pt.make_so_far("root", job, pieces, lambda *a: None)
    finally:
        pt.pack_in_pieces = real
    assert sorted(f.name for f in folder.glob("part-*.mp4")) == before
    assert not list(folder.glob("sofar-*")), "its own working files are cleared away"


def test_the_lens_angle_cannot_orphan_a_job(tmp_path):
    """Nudging the lens spinner by one notch used to point the job at an empty folder, and
    hours of finished chunks vanished. The angle has nothing to do with the mattes."""
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    at_180 = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
                 width=320, height=180, fps=30.0, frames=3000, fov=180)
    at_155 = replace_fov(at_180, 155, tmp_path)
    assert passthrough.work_folder(at_180) == passthrough.work_folder(at_155)


def replace_fov(job, fov, tmp_path):
    from dataclasses import replace
    return replace(job, fov=fov,
                   output=str(tmp_path / f"scene_FISHEYE{fov}_alpha.mp4"))


def test_chunks_in_a_folder_an_older_version_used_are_brought_over(tmp_path):
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=3000)
    pieces = passthrough.plan_slices(3000, None, chunk=1000)

    # Three chunks, in the folder the old naming would have used.
    old = tmp_path / ".scene_FISHEYE180_alpha.work"
    old.mkdir()
    for piece in pieces:
        passthrough.blank_segment(job, piece.kept, passthrough.part_file(old, piece))

    assert passthrough.so_far(job, pieces)[1] == 0, "not seen where it now looks"
    assert passthrough.gather_older_work(job, pieces) == 3
    assert passthrough.so_far(job, pieces)[1] == 3, "and now they are"
    assert passthrough.gather_older_work(job, pieces) == 0, "and not again"


def test_gathering_ignores_chunks_of_the_wrong_length(tmp_path):
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=3000)
    pieces = passthrough.plan_slices(3000, None, chunk=1000)
    old = tmp_path / ".scene_FISHEYE180_alpha.work"
    old.mkdir()
    # matted under a different chunk size, so the pieces do not line up
    passthrough.blank_segment(job, 500, passthrough.part_file(old, pieces[0]))
    assert passthrough.gather_older_work(job, pieces) == 0


# --------------------------------------------------------------------------- the same job

def test_the_settings_are_written_down_beside_the_chunks(tmp_path):
    """So a job picked up later is provably the same job, on any machine."""
    job, pieces, folder = a_half_done_job(tmp_path)
    passthrough.save_settings(job, pieces)
    saved = passthrough.read_settings(job)
    assert saved["model"] == job.model and saved["detail"] == job.detail
    assert saved["plan"], "including how the video was cut up"
    assert passthrough.settings_differences(job, pieces, saved) == []


def test_settings_that_change_the_chunks_are_noticed(tmp_path):
    from dataclasses import replace as _replace
    job, pieces, _folder = a_half_done_job(tmp_path)
    passthrough.save_settings(job, pieces)
    saved = passthrough.read_settings(job)

    sharper = _replace(job, detail="finest")
    assert any("detail" in d for d in passthrough.settings_differences(sharper, pieces, saved))
    slower = _replace(job, model="resnet50")
    assert any("model" in d for d in passthrough.settings_differences(slower, pieces, saved))


def test_settings_that_do_not_touch_the_chunks_are_ignored(tmp_path):
    """The lens angle and the quality of the copy change the copy, not the mattes - they
    must not scare anyone into thinking the work is wasted."""
    from dataclasses import replace as _replace
    job, pieces, _folder = a_half_done_job(tmp_path)
    passthrough.save_settings(job, pieces)
    saved = passthrough.read_settings(job)
    for changed in (_replace(job, fov=155), _replace(job, crf=24)):
        assert passthrough.settings_differences(changed, pieces, saved) == []


def test_cutting_the_video_up_differently_is_noticed(tmp_path):
    job, pieces, _folder = a_half_done_job(tmp_path)
    passthrough.save_settings(job, pieces)
    saved = passthrough.read_settings(job)
    other = passthrough.plan_slices(job.frames, None, chunk=500)
    differs = passthrough.settings_differences(job, other, saved)
    assert any("cut up differently" in d for d in differs), differs


# --------------------------------------------------------------------------- resuming a stage

def test_the_joined_mask_is_not_made_twice(tmp_path):
    """After the chunks comes an hour of work. Losing all of it to a crash at the end is
    what stage-by-stage resuming exists to prevent."""
    job, pieces, folder = a_half_done_job(tmp_path)
    for piece in pieces:
        if not piece.blank and not passthrough.finished(
                passthrough.part_file(folder, piece), piece.kept):
            passthrough.blank_segment(job, piece.kept, passthrough.part_file(folder, piece))

    said = []
    first = passthrough.build_matte(job, pieces, lambda p: passthrough.part_file(folder, p),
                                    folder, lambda s, f, d: said.append(s))
    assert passthrough.frame_count(first) == job.frames
    stamp = first.stat().st_mtime_ns

    again = []
    second = passthrough.build_matte(job, pieces, lambda p: passthrough.part_file(folder, p),
                                     folder, lambda s, f, d: again.append(s))
    assert second.stat().st_mtime_ns == stamp, "it should not have been rebuilt"
    assert any("already" in line for line in again), again


def test_the_cut_picture_is_not_made_twice(tmp_path):
    job, pieces, folder = a_half_done_job(tmp_path, real_source=True)
    keep = [p for p in pieces[:3]]
    into = tmp_path / "cut.mp4"

    first = passthrough.cut_to(job.source, keep, into, 160, 120, job.fps)
    stamp = first.stat().st_mtime_ns
    second = passthrough.cut_to(job.source, keep, into, 160, 120, job.fps)
    assert second.stat().st_mtime_ns == stamp, "the whole cut should be reused"


def test_a_cut_that_died_half_way_keeps_what_it_had(tmp_path, monkeypatch):
    """Cutting 8K takes minutes a stretch; a crash must not send it back to the start."""
    job, pieces, folder = a_half_done_job(tmp_path, real_source=True)
    keep = list(pieces[:3])
    into = tmp_path / "cut.mp4"
    scratch = into.parent / f"{into.stem}.parts"

    calls = {"n": 0}
    real = passthrough._ffmpeg_watched

    def die_on_the_third(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise passthrough.Unavailable("pretend the machine fell over")
        return real(*args, **kwargs)

    monkeypatch.setattr(passthrough, "_ffmpeg_watched", die_on_the_third)
    with pytest.raises(passthrough.Unavailable):
        passthrough.cut_to(job.source, keep, into, 160, 120, job.fps, lambda *a: None)

    survived = sorted(scratch.glob("cut-*.mp4"))
    assert len(survived) == 2, "the two stretches that finished are still there"

    monkeypatch.setattr(passthrough, "_ffmpeg_watched", real)
    passthrough.cut_to(job.source, keep, into, 160, 120, job.fps, lambda *a: None)
    assert passthrough.frame_count(into) == sum(p.kept for p in keep)
    assert not scratch.exists(), "and cleared away once it worked"


# --------------------------------------------------------------------------- packing in pieces

def test_the_tail_is_cut_into_pieces_that_cover_everything(tmp_path, monkeypatch):
    """Projecting and packing has no frame-to-frame state, so it can be split freely - no
    run-up, nothing lost, and any machine can take any piece."""
    job = Job(source=str(tmp_path / "s.mp4"), output=str(tmp_path / "o.mp4"),
              width=320, height=180, fps=30.0, frames=2500, pack_chunk=1000)
    matte = passthrough.blank_segment(job, 2500, tmp_path / "matte.mp4")

    asked = []

    class FakeRun:
        def __init__(self, root, spec):
            self.spec, self.result, self.error, self.noise = spec, None, None, []

        def lines(self):
            asked.append((self.spec["start"], self.spec["end"]))
            passthrough.blank_segment(job, self.spec["end"] - self.spec["start"] + 1,
                                      Path(self.spec["output"]))
            self.result = {"output": self.spec["output"]}
            return iter(())

        def cleanup(self):
            pass

    monkeypatch.setattr(passthrough, "Run", FakeRun)
    out = passthrough.pack_in_pieces("root", job, job.source, matte,
                                     tmp_path / "packed.mp4", lambda *a: None, tmp_path,
                                     chunk=1000)
    assert asked == [(1, 1000), (1001, 2000), (2001, 2500)], asked
    assert passthrough.frame_count(out) == 2500, "every frame, exactly once"


def test_a_piece_already_packed_is_not_packed_again(tmp_path, monkeypatch):
    job = Job(source=str(tmp_path / "s.mp4"), output=str(tmp_path / "o.mp4"),
              width=320, height=180, fps=30.0, frames=2000, pack_chunk=1000)
    matte = passthrough.blank_segment(job, 2000, tmp_path / "matte.mp4")
    passthrough.blank_segment(job, 1000, tmp_path / "packed-000.mp4")

    asked = []

    class FakeRun:
        def __init__(self, root, spec):
            self.spec, self.result, self.error = spec, None, None

        def lines(self):
            asked.append(self.spec["start"])
            passthrough.blank_segment(job, self.spec["end"] - self.spec["start"] + 1,
                                      Path(self.spec["output"]))
            self.result = {"output": self.spec["output"]}
            return iter(())

        def cleanup(self):
            pass

    monkeypatch.setattr(passthrough, "Run", FakeRun)
    passthrough.pack_in_pieces("root", job, job.source, matte, tmp_path / "packed.mp4",
                               lambda *a: None, tmp_path, chunk=1000)
    assert asked == [1001], "only the piece that was missing"


def test_no_piece_size_means_one_pass_over_the_lot(tmp_path, monkeypatch):
    job = Job(source=str(tmp_path / "s.mp4"), output=str(tmp_path / "o.mp4"),
              width=320, height=180, fps=30.0, frames=900)
    matte = passthrough.blank_segment(job, 900, tmp_path / "matte.mp4")
    asked = []

    class FakeRun:
        def __init__(self, root, spec):
            self.spec, self.result, self.error = spec, None, None

        def lines(self):
            asked.append((self.spec["start"], self.spec["end"]))
            passthrough.blank_segment(job, 900, Path(self.spec["output"]))
            self.result = {"output": self.spec["output"]}
            return iter(())

        def cleanup(self):
            pass

    monkeypatch.setattr(passthrough, "Run", FakeRun)
    passthrough.pack_in_pieces("root", job, job.source, matte, tmp_path / "packed.mp4",
                               lambda *a: None, tmp_path, chunk=0)
    assert asked == [(1, 900)]


# --------------------------------------------------------------------------- sound

def a_video_with_sound(tmp_path, name="src.mp4", frames=600):
    path = tmp_path / name
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-frames:v", str(frames), "-c:v", "libopenh264", "-b:v", "2M",
                    "-c:a", "aac", "-shortest", "-y", str(path)], timeout=900)
    return path


def kinds(path):
    said = paths.run_tool([paths.tool("ffprobe"), "-v", "error", "-show_entries",
                           "stream=codec_type", "-of", "csv=p=0", str(path)], timeout=300)
    return [line for line in (said.stdout or b"").decode().split() if line]


def test_the_cut_keeps_the_sound(tmp_path):
    """A copy that plays silently in a headset is not much of a copy."""
    src = a_video_with_sound(tmp_path)
    assert "audio" in kinds(src), "the test source needs sound to prove anything"
    keep = [Slice(0, 1, 101, 300), Slice(1, 1, 401, 500)]
    cut = passthrough.cut_to(src, keep, tmp_path / "cut.mp4", 320, 180, 30.0)
    assert "audio" in kinds(cut)
    assert passthrough.frame_count(cut) == sum(p.kept for p in keep)


def test_joining_masks_does_not_expect_sound(tmp_path):
    """The same join handles masks, which have none, and must not choke on that."""
    job = Job(source="x.mp4", output=str(tmp_path / "o.mp4"),
              width=320, height=180, fps=30.0)
    parts = [passthrough.blank_segment(job, 40, tmp_path / f"b{n}.mp4") for n in range(3)]
    joined = passthrough.join(parts, tmp_path / "joined.mp4")
    assert kinds(joined) == ["video"]
    assert passthrough.frame_count(joined) == 120


def test_chunks_from_a_shared_job_are_gathered_too(tmp_path):
    """Chunks landed in two different places depending on whether the work was shared.
    Both are hours of matting; a restart must find either."""
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=3000)
    pieces = passthrough.plan_slices(3000, None, chunk=1000)

    old = tmp_path / ".scene_FISHEYE180_alpha.work"
    # one made alone, in the old working folder
    old.mkdir()
    passthrough.blank_segment(job, pieces[0].kept, passthrough.part_file(old, pieces[0]))
    # two made by a shared job, inside it, named the way the farm used to
    out = old / "jobs" / "20260927-000000-abcdef" / "out"
    out.mkdir(parents=True)
    for piece in pieces[1:]:
        passthrough.blank_segment(job, piece.kept, out / f"{piece.index}.mp4")

    assert passthrough.so_far(job, pieces)[1] == 0
    assert passthrough.gather_older_work(job, pieces) == 3, "all three, from both places"
    assert passthrough.so_far(job, pieces)[1] == 3


# --------------------------------------------------------------------------- knowing the machines

def test_a_machine_can_measure_its_link(tmp_path):
    """Which machine should do the tail is decided by the link, not the graphics card."""
    found = passthrough.measure_link(tmp_path, megabytes=16)
    assert found["write_mbs"] > 0 and found["read_mbs"] > 0
    assert not list(tmp_path.glob(".speed-*")), "the test file is cleared away"


def test_machines_tell_each_other_what_they_can_do(tmp_path):
    desktop = farm.Share(tmp_path / "farm", machine="desktop")
    laptop = farm.Share(tmp_path / "farm", machine="laptop")
    desktop.publish_machine({"gpu": "big", "matte_fps": 42.0,
                             "read_mbs": 180.0, "write_mbs": 150.0})
    laptop.publish_machine({"gpu": "small", "matte_fps": 15.0,
                            "read_mbs": 24.0, "write_mbs": 22.0})

    names = sorted(m["machine"] for m in desktop.machines())
    assert names == ["desktop", "laptop"], "each reads the other's"
    assert all("at" in m for m in desktop.machines()), "and when it was measured"


def test_the_tail_is_put_on_the_best_connected_machine(tmp_path):
    """Not the fastest card: that stage moves tens of gigabytes and computes little."""
    place = farm.Share(tmp_path / "farm", machine="quick-card-slow-line")
    place.publish_machine({"matte_fps": 90.0, "read_mbs": 24.0, "write_mbs": 22.0})
    farm.Share(place.root, machine="slow-card-quick-line").publish_machine(
        {"matte_fps": 12.0, "read_mbs": 180.0, "write_mbs": 170.0})
    assert place.best_for_the_tail()["machine"] == "slow-card-quick-line"


def test_a_machine_that_has_not_been_measured_is_not_chosen(tmp_path):
    place = farm.Share(tmp_path / "farm", machine="unmeasured")
    place.publish_machine({"gpu": "something"})
    assert place.best_for_the_tail() is None
    farm.Share(place.root, machine="measured").publish_machine(
        {"read_mbs": 50.0, "write_mbs": 50.0})
    assert place.best_for_the_tail()["machine"] == "measured"


def test_measuring_again_replaces_the_old_answer(tmp_path):
    place = farm.Share(tmp_path / "farm", machine="desktop")
    place.publish_machine({"read_mbs": 10.0, "write_mbs": 10.0})
    place.publish_machine({"read_mbs": 99.0, "write_mbs": 99.0})
    assert len(place.machines()) == 1
    assert place.machines()[0]["read_mbs"] == 99.0


# --------------------------------------------------------------------------- sharing the tail

def test_the_tail_is_cut_without_any_warm_up():
    """Matting needs a run-up because the model carries state. Projecting and packing do
    not, so their pieces can be any size at no cost at all."""
    pieces = passthrough.tail_pieces(10000, 2500)
    assert [(p.start, p.end) for p in pieces] == [(1, 2500), (2501, 5000),
                                                  (5001, 7500), (7501, 10000)]
    assert all(p.lead_in == 0 for p in pieces), "no frames thrown away"
    assert sum(p.kept for p in pieces) == 10000


def test_a_pack_job_is_marked_as_one(tmp_path):
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    matte = passthrough.blank_segment(job, 3000, tmp_path / "matte.mp4")

    matting = place.post(job, pieces)
    packing = place.post(job, passthrough.tail_pieces(3000, 1000), stage="pack",
                         extra={"matte": str(matte), "picture": job.source})
    assert place.stage_of(matting) == "matte"
    assert place.stage_of(packing) == "pack"
    assert place.details_of(packing)["matte"] == str(matte)


def test_the_two_kinds_of_work_cannot_be_mistaken_for_each_other(tmp_path):
    """Both live beside the video, so both survive a crash - but a mask and a finished
    piece of the copy must never be confused."""
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    tail = passthrough.tail_pieces(3000, 1000)
    matting = place.post(job, pieces)
    matte_file = place.segment_of(matting, pieces[0])
    # Posting the packing retires the matting queue, which is right - by then it is done -
    # so where its chunks live is read before the switch.
    packing = place.post(job, tail, stage="pack",
                         extra={"matte": "m.mp4", "picture": job.source})
    packed_file = place.segment_of(packing, tail[0])
    assert matte_file.parent == passthrough.work_folder(job), "kept with the video"
    assert packed_file.parent == passthrough.work_folder(job), "and so are these"
    assert matte_file.name.startswith("part-")
    assert packed_file.name.startswith("packed-")
    assert matte_file != packed_file, "same index, different work, different file"


def test_a_blank_stretch_is_timed_like_the_real_segments(tmp_path):
    """A blank written at its own time base poisons the join it is first in.

    "59.940060" is not 60000/1001, and ffmpeg picks a 1/11988 time base for it while the
    segments cut from the source carry 1/60000. Joining without re-encoding takes the time
    base from the first piece, so every real packet is then read in the wrong units.
    """
    video = tmp_path / "at-5994.mp4"
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x64:rate=60000/1001", "-frames:v", "60",
                    "-c:v", "libopenh264", "-y", str(video)], timeout=300)
    job = Job(source=str(video), output=str(tmp_path / "out.mp4"),
              width=64, height=64, fps=60000 / 1001, frames=60)
    blank = passthrough.blank_segment(job, 60, tmp_path / "blank.mp4")

    assert _time_base(blank) == _time_base(job.source), "same clock as the picture"


def test_a_mask_with_the_wrong_clock_is_refused(tmp_path):
    """Frames all present and in the right order is not enough - the length must fit too."""
    job, _pieces, _folder = a_half_done_job(tmp_path, real_source=True)
    stretched = tmp_path / "stretched.mp4"
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "color=c=white:s=64x64:r=6", "-frames:v", "120", "-c:v", "libopenh264",
                    "-y", str(stretched)], timeout=300)

    # 120 frames that claim 20 seconds, where 30 a second would make it 4.
    with pytest.raises(passthrough.Unavailable, match="would not line up"):
        passthrough.check_timing(stretched, 120, 30.0)

    passthrough.check_timing(stretched, 120, 6.0)   # honest about its own length


def _time_base(path) -> str:
    said = paths.run_tool([paths.tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=time_base", "-of", "default=nk=1:nw=1",
                           str(path)], timeout=60)
    return (said.stdout or b"").decode().strip()


def test_a_finished_mask_means_no_shares_are_handed_out(tmp_path, monkeypatch):
    """Not one chunk is worth matting once the mask they build is made and the right length.

    A chunk lost since the mask was joined used to send every machine back to matting it,
    hours of work to arrive at a file already sitting in the same folder.
    """
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "matte.mp4").write_bytes(b"x" * 2048)
    total = sum(p.kept for p in pieces)
    monkeypatch.setattr(passthrough, "frame_count",
                        lambda path: total if Path(path).name == "matte.mp4" else 0)

    job_id = place.post(job, pieces)

    assert place.claim() is None, "nothing left to hand out"
    assert place.state(job_id)["complete"]


def test_joining_says_how_far_along_it_is(tmp_path):
    """Twenty gigabytes in silence is indistinguishable from twenty gigabytes stuck."""
    made = []
    for n in range(3):
        clip = tmp_path / f"p{n}.mp4"
        paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=64x64:rate=30", "-frames:v", "60", "-c:v",
                        "libopenh264", "-y", str(clip)], timeout=300)
        made.append(clip)

    seen: list[tuple[float, int]] = []
    passthrough.join(made, tmp_path / "all.mp4", lambda share, frame: seen.append((share, frame)))

    assert seen, "it said nothing at all"
    assert all(a[0] <= b[0] for a, b in zip(seen, seen[1:])), "never goes backwards"
    assert seen[-1][0] <= 1.0
    assert passthrough.frame_count(tmp_path / "all.mp4") == 180


def test_a_packed_piece_that_comes_out_short_is_failed_not_retried(tmp_path, monkeypatch):
    """Silence was the real damage: the piece just went round again, for hours."""
    job, _pieces, _folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    tail = passthrough.tail_pieces(3000, 1000)
    job_id = place.post(job, tail, stage="pack",
                        extra={"matte": "m.mp4", "picture": job.source})

    def short(_root, _job, _picture, _matte, _piece, into, _report):
        Path(into).write_bytes(b"x" * 4096)      # there, but not long enough
        return Path(into)

    monkeypatch.setattr(passthrough, "pack_piece", short)
    monkeypatch.setattr(passthrough, "frame_count", lambda _p: 400)

    helper = farm.Helper(place, tmp_path / "install", work_dir=tmp_path / "scratch")
    assert helper.one() is True

    state = place.state(job_id)
    assert state["failed"] == 1, "it must be marked failed, not quietly freed"
    assert any("400" in why and "1,000" in why for why in state["failures"]), state["failures"]


def test_uneven_frame_timing_is_spotted(tmp_path):
    """A mask laid over the picture by time only lines up while the frames are evenly spaced.

    Some studios ship variable frame rate. The mask then drifts further out of step the
    longer it plays, and it does not show until the whole thing is finished.
    """
    steady = tmp_path / "steady.mp4"
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x64:rate=30:duration=20", "-c:v", "libopenh264",
                    "-y", str(steady)], timeout=300)
    uneven = tmp_path / "uneven.mp4"
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x64:rate=30:duration=20", "-vf",
                    "setpts='if(lt(N,150), N/30/TB, (150+(N-150)*3)/30/TB)'",
                    "-fps_mode", "passthrough", "-c:v", "libopenh264",
                    "-y", str(uneven)], timeout=300)

    kind, why = passthrough.frame_timing(steady)
    assert kind == "cfr", why
    kind, why = passthrough.frame_timing(uneven)
    assert kind == "vfr", why
    assert "differ" in why, "it must say what it found, not just refuse"

    assert passthrough.frame_timing(tmp_path / "nothing.mp4")[0] == "unknown"


def test_the_tail_is_planned_to_the_shorter_of_mask_and_picture(tmp_path):
    """The mask came out one frame longer than the video, and that one frame cost a night.

    The packer clamps each piece to the picture's length, so a last piece planned from the
    mask asked for 291 frames and could only ever make 290. Never the right length, never
    accepted, handed out again for hours, and the join never ran.
    """
    def clip(name, frames):
        made = tmp_path / name
        paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=64x64:rate=30", "-frames:v", str(frames),
                        "-c:v", "libopenh264", "-y", str(made)], timeout=300)
        return made

    mask, picture = clip("mask.mp4", 101), clip("picture.mp4", 100)

    assert passthrough.frame_count(mask) == 101
    assert passthrough.packable_frames(mask, picture) == 100, "never more than the picture"
    assert passthrough.packable_frames(picture, mask) == 100, "nor more than the mask"

    last = passthrough.tail_pieces(passthrough.packable_frames(mask, picture), 40)[-1]
    assert last.end == 100 and last.kept == 20


def test_a_finished_mask_stops_the_pieces_being_made_again(tmp_path):
    """Hours of matting were spent arriving at a file that was already sitting there."""
    job, pieces, folder = a_half_done_job(tmp_path)
    total = sum(p.kept for p in pieces)
    matte = folder / "matte.mp4"
    matte.parent.mkdir(parents=True, exist_ok=True)
    matte.write_bytes(b"x" * 2048)

    asked: list[int] = []

    def never(piece):
        asked.append(piece.index)
        raise AssertionError("nothing should be matted when the mask is already made")

    def counted(path, _wanted=None, _matte=matte):
        return total if Path(path) == _matte else 0

    import blueshelf.passthrough as module
    was, module.frame_count = module.frame_count, lambda p: counted(p)
    try:
        got = passthrough.build_matte(job, pieces, never, folder,
                                      lambda _s, _f, _d: None)
    finally:
        module.frame_count = was

    assert got == matte
    assert asked == [], "not one piece was asked for"


def test_filing_a_packed_piece_does_not_land_on_the_matte_chunk(tmp_path):
    """Filing a finished piece must put it where segment_of says, not where it guesses.

    finish() used to work the name out for itself, so every stage landed on part-NNN.mp4:
    the packing wrote its finished video straight over the matte chunk it had been made
    from, and the join then went looking for packed-NNN.mp4 and found nothing.
    """
    job, _pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    tail = passthrough.tail_pieces(3000, 1000)
    job_id = place.post(job, tail, stage="pack",
                        extra={"matte": "m.mp4", "picture": job.source})
    chunk = folder / "part-000.mp4"
    chunk.write_bytes(b"the mask this piece was made from")

    _got_id, piece, _job = place.claim()
    staging = tmp_path / "just-packed.mp4"
    staging.write_bytes(b"the finished picture")
    place.finish(job_id, piece.index, staging)

    assert (folder / "packed-000.mp4").read_bytes() == b"the finished picture"
    assert chunk.read_bytes() == b"the mask this piece was made from", "matte untouched"


def test_filing_a_join_leaves_the_finished_copy_where_it_belongs(tmp_path):
    """The join writes the copy itself, so filing it must not drag it into the work folder."""
    job, _pieces, _folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, [Slice(0, 1, 1, 1)], stage="join",
                        extra={"inputs": [], "output": job.output})
    _got_id, piece, _job = place.claim()
    made = pathlib.Path(job.output)
    made.write_bytes(b"the whole film")

    place.finish(job_id, piece.index, made)

    assert made.read_bytes() == b"the whole film", "left exactly where the join put it"
    assert place.state(job_id)["complete"]


def test_finishing_a_film_comes_before_starting_another(tmp_path):
    """A film one step from done must not wait behind every chunk of the next one.

    Work was handed out oldest first, and a pack job is filed when packing begins - so it
    sorted after a second film queued earlier with thirty chunks still to matte, and a film
    that was all but finished sat for most of a day.
    """
    import time as clock
    job, pieces, _folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")

    other_video = tmp_path / "second.mp4"
    other_video.write_bytes(b"x")
    second = Job(source=str(other_video), output=str(tmp_path / "second-out.mp4"),
                 width=320, height=180, fps=30.0, frames=7400)
    matting = place.post(second, passthrough.plan_slices(7400, None, chunk=1000))
    clock.sleep(1.1)                                       # so the ids sort by time
    packing = place.post(job, passthrough.tail_pieces(3000, 1000), stage="pack",
                         extra={"matte": "m.mp4", "picture": job.source})

    assert place.jobs() == sorted([matting, packing]), "filed in that order"
    assert place.by_urgency()[0] == packing, "but the nearly finished one goes first"

    got_id, _piece, _job = place.claim()
    assert got_id == packing, "the first share handed out finishes a film"


def test_tail_pieces_are_claimed_like_any_other(tmp_path):
    job, _pieces, _folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    other = farm.Share(place.root, machine="laptop")
    job_id = place.post(job, passthrough.tail_pieces(6000, 1000), stage="pack",
                        extra={"matte": "m.mp4", "picture": job.source})
    taken = []
    while True:
        got = place.claim() if len(taken) % 2 == 0 else other.claim()
        if got is None:
            break
        taken.append(got[1].index)
    assert sorted(taken) == list(range(6)), "shared out, none done twice"


# --------------------------------------------------------------------------- no doing it twice

def test_work_already_on_disk_is_never_handed_out_again(tmp_path):
    """A queue written an hour ago still says "waiting" for chunks finished since. Handing
    one out costs an hour and produces a file that already exists."""
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, pieces)
    assert place.state(job_id)["done"] == 7

    # Another attempt finishes one of the pieces this queue still thinks is waiting.
    later = next(p for p in pieces if not p.blank
                 and not passthrough.finished(passthrough.part_file(folder, p), p.kept))
    passthrough.blank_segment(job, later.kept, passthrough.part_file(folder, later))

    taken = []
    while (got := place.claim()) is not None:
        taken.append(got[1].index)
        place.finish(job_id, got[1].index, _a_file(tmp_path, got[1].index))
    assert later.index not in taken, "it was already done; nobody should redo it"
    assert place.state(job_id)["complete"]


def test_starting_again_takes_down_the_old_queue(tmp_path):
    """Two live queues for one video had both machines working the same chunks."""
    job, pieces, folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    first = place.post(job, pieces)
    second = place.post(job, pieces)
    assert place.jobs() == [second], "only the newest attempt is left standing"
    assert first not in place.jobs()


def test_another_video_is_left_alone_when_one_is_restarted(tmp_path):
    job, pieces, _folder = a_half_done_job(tmp_path)
    other_video = tmp_path / "other.mp4"
    other_video.write_bytes(b"x")
    other = Job(source=str(other_video), output=str(tmp_path / "other_FISHEYE180_alpha.mp4"),
                width=320, height=180, fps=30.0, frames=7400)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    keep = place.post(other, pieces)
    place.post(job, pieces)
    assert keep in place.jobs(), "a different film's queue is not ours to take down"


# --------------------------------------------------------------------------- where the join runs

def a_join_job(tmp_path, machine="desktop"):
    job, pieces, _folder = a_half_done_job(tmp_path)
    place = farm.Share(tmp_path / "farm", machine=machine)
    job_id = place.post(job, [Slice(0, 1, 1, 1)], stage="join",
                        extra={"inputs": [], "output": str(tmp_path / "out.mp4")})
    return place, job_id


def test_the_join_waits_for_the_machine_with_the_better_link(tmp_path):
    """Joining is nothing but moving bytes, so it belongs on the best link - not on
    whoever happened to press Start."""
    place, job_id = a_join_job(tmp_path, machine="on-wifi")
    place.publish_machine({"read_mbs": 24.0, "write_mbs": 22.0})
    farm.Share(place.root, machine="on-a-wire").publish_machine(
        {"read_mbs": 180.0, "write_mbs": 170.0})

    assert not place.my_turn_to_join(job_id), "the slow one should leave it alone"
    assert place.claim() is None, "and not take it"
    wired = farm.Share(place.root, machine="on-a-wire")
    assert wired.my_turn_to_join(job_id)
    assert wired.claim() is not None, "the quick one takes it"


def test_the_join_is_not_left_undone_if_that_machine_never_comes(tmp_path):
    """A job unfinished because a computer was switched off is worse than a slow join."""
    place, job_id = a_join_job(tmp_path, machine="on-wifi")
    place.publish_machine({"read_mbs": 24.0, "write_mbs": 22.0})
    farm.Share(place.root, machine="never-turned-on").publish_machine(
        {"read_mbs": 999.0, "write_mbs": 999.0})
    assert not place.my_turn_to_join(job_id)

    # Wind the job back so it has been waiting past the limit.
    record = place.job_dir(job_id) / "job.json"
    told = json.loads(record.read_text())
    told["posted_at"] = time.time() - farm.JOIN_WAIT - 60
    record.write_text(json.dumps(told))
    place._records.pop(job_id, None)
    assert place.my_turn_to_join(job_id), "after a while, anyone may"


def test_a_stale_measurement_does_not_hold_the_join_up(tmp_path):
    place, job_id = a_join_job(tmp_path, machine="here-now")
    place.publish_machine({"read_mbs": 24.0, "write_mbs": 22.0})
    quick = farm.Share(place.root, machine="measured-last-week")
    quick.publish_machine({"read_mbs": 999.0, "write_mbs": 999.0})
    stale = quick.machines_dir / "measured-last-week.json"
    told = json.loads(stale.read_text())
    told["at"] = time.time() - farm.MEASUREMENT_KEEPS - 60
    stale.write_text(json.dumps(told))
    assert place.my_turn_to_join(job_id), "an old number is not a promise"


def test_with_nothing_measured_whoever_is_there_joins(tmp_path):
    place, job_id = a_join_job(tmp_path)
    assert place.my_turn_to_join(job_id)
    assert place.claim() is not None
