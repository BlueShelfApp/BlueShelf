"""A share that fails once is tried again by whichever machine is looking.

Breezy Bri's packing finished 32 of its 33 pieces. The other came out 1,986 frames long of
5,000 - the NAS had refused connections for a moment - and was rightly failed. Then nothing:
only the machine that posted a job ever retried, that machine was only helping, and both
screens showed a finished progress bar with nobody saying a share was waiting on a person.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from blueshelf import farm, passthrough, paths
from blueshelf.db import Database
from blueshelf.passthrough import Job


def _posted(tmp_path, machine="desktop", stage="matte"):
    place = farm.Share(tmp_path / "farm", machine=machine)
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=3000)
    if stage == "pack":
        pieces = passthrough.tail_pieces(3000, 1000)
        job_id = place.post(job, pieces, stage="pack",
                            extra={"matte": "m.mp4", "picture": job.source})
    else:
        job_id = place.post(job, passthrough.split(3000, 1, 0, chunk=1000))
    return place, job_id


def _rested(place, job_id):
    """Age every failed file past the rest, as if the minutes had gone by."""
    old = time.time() - farm.RETRY_AFTER - 5
    for _index, kind, _tag, path in place._files(job_id):
        if kind == "failed":
            os.utime(path, (old, old))


# --------------------------------------------------------------------------- the rule

def test_one_failure_is_put_back_after_a_rest_and_a_second_is_left_for_a_person(tmp_path):
    place, job_id = _posted(tmp_path)
    _id, piece, _job = place.claim()
    place.fail(job_id, piece.index, "came out 1,986 frames where 1,000 were asked for")

    state = place.state(job_id)
    assert state["failed"] == 1 and state["retrying"] == 1, "due another go, not given up on"
    assert place.retry_on_its_own() == [], "not straight away - whatever cut it short may not have passed"

    _rested(place, job_id)
    said = place.retry_on_its_own()
    assert len(said) == 1 and "Share 1 of scene.mp4" in said[0] and "1,986" in said[0], said
    assert place.state(job_id)["failed"] == 0

    # Taken by the other machine this time: the count goes with the share, not the machine.
    other = farm.Share(tmp_path / "farm", machine="laptop")
    _id, again, _job = other.claim()
    assert again.index == piece.index
    other.fail(job_id, again.index, "the same thing again")
    state = place.state(job_id)
    assert state["failed"] == 1 and state["retrying"] == 0, "twice is for a person to look at"
    _rested(place, job_id)
    assert place.retry_on_its_own() == [] and other.retry_on_its_own() == []

    assert place.retry_failed(job_id) == 1, "the button still puts it back"


def test_a_failure_written_before_the_count_counts_as_one(tmp_path):
    """The share that is failed on the NAS right now was written by the build before this."""
    place, job_id = _posted(tmp_path)
    parts = place.parts_dir(job_id)
    (parts / "1.waiting").unlink()
    (parts / "1.failed").write_text("helper@WORKSTATION\ncame out 1,986 frames where 5,000 were "
                                    "asked for", encoding="utf-8")

    assert place.state(job_id)["retrying"] == 1
    assert "1,986" in place.state(job_id)["failures"][0], "the reason still shows, not the count"
    _rested(place, job_id)
    assert len(place.retry_on_its_own()) == 1

    _id, piece, _job = place.claim()
    while piece.index != 1:                       # the others are there too; find the one
        place.finish(job_id, piece.index, _stand_in(tmp_path, piece.index))
        _id, piece, _job = place.claim()
    place.fail(job_id, piece.index, "and again")
    assert place.state(job_id)["retrying"] == 0, "its earlier failure was remembered"


def _stand_in(tmp_path, index):
    made = tmp_path / f"stand-in-{index}.mp4"
    made.write_bytes(b"x")
    return made


def test_a_share_put_back_politely_keeps_its_count(tmp_path):
    """Stopping part-way hands a share back; that is not a fresh start for its count."""
    place, job_id = _posted(tmp_path)
    _id, piece, _job = place.claim()
    place.fail(job_id, piece.index, "a moment's trouble")
    _rested(place, job_id)
    place.retry_on_its_own()
    _id, piece, _job = place.claim()
    place.release(job_id, piece.index)
    _id, piece, _job = place.claim()
    place.fail(job_id, piece.index, "trouble again")
    assert place.state(job_id)["retrying"] == 0


# --------------------------------------------------------------------------- a helper does it

def test_a_helper_tries_a_rested_failure_again_itself(tmp_path, monkeypatch):
    """The case that sat for an hour: every machine only helping, one piece failed."""
    place, job_id = _posted(tmp_path, stage="pack")
    lengths = {"short": True}

    def pack(_root, _job, _picture, _matte, piece, into, _report):
        Path(into).write_bytes(b"x" * 4096)
        return Path(into)

    monkeypatch.setattr(passthrough, "pack_piece", pack)
    monkeypatch.setattr(passthrough, "frame_count",
                        lambda _p: 400 if lengths["short"] else 1000)
    said = []
    helper = farm.Helper(place, tmp_path / "install", report=said.append,
                         work_dir=tmp_path / "scratch")

    assert helper.one() is True
    assert place.state(job_id)["failed"] == 1
    lengths["short"] = False                      # the NAS is back
    while helper.one():                           # the rest are done; the failure rests
        pass
    assert place.state(job_id)["failed"] == 1 and place.state(job_id)["done"] == 2

    _rested(place, job_id)
    assert helper.one() is True, "it should have put the failure back and done it"
    assert any("trying it once more" in line for line in said), said
    assert place.state(job_id)["complete"], "and the job can now be joined"


# --------------------------------------------------------------------------- what is shown

@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, monkeypatch, qt_app):
    data = tmp_path / "data"
    (data / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    from blueshelf.passthrough_dialog import PassthroughDialog
    made = PassthroughDialog(Database(str(data / "library.db")), [])
    yield made
    made.shut_down()
    made.deleteLater()


def _rows(widget):
    return [widget.item(n).text() for n in range(widget.count())]


def test_the_helping_tab_says_a_failure_will_be_retried_then_offers_the_button(tmp_path, window):
    place, job_id = _posted(tmp_path)
    for _ in range(3):
        _id, piece, _job = place.claim()
        if piece.index == 1:
            place.fail(job_id, piece.index, "came out 1,986 frames where 1,000 were asked for")
        else:
            place.finish(job_id, piece.index, _stand_in(tmp_path, piece.index))
    root = str(tmp_path / "farm")
    window.help_share_line.setText(root)

    window._fill_queue(root)
    rows = _rows(window.queue)
    assert any("1 failed" in row and "on its own" in row for row in rows), rows
    assert any("why:" in row and "1,986" in row for row in rows), rows
    assert window.help_retry_btn.isEnabled()

    _rested(place, job_id)
    place.retry_on_its_own()
    _id, piece, _job = place.claim()
    place.fail(job_id, piece.index, "and again")
    window._fill_queue(root)
    rows = _rows(window.queue)
    assert any("failed twice" in row and "Try the failed chunks again" in row for row in rows), rows

    window._retry_failed_here()
    assert place.state(job_id)["waiting"] == 1
    assert not window.help_retry_btn.isEnabled(), "nothing left to retry"
    assert "back in the queue" in window.help_log.toPlainText()


def test_a_job_one_failed_share_short_is_not_called_ready_to_join(tmp_path, window):
    place, job_id = _posted(tmp_path)
    for _ in range(3):
        _id, piece, _job = place.claim()
        if piece.index == 2:
            place.fail(job_id, piece.index, "nowhere to write it")
        else:
            place.finish(job_id, piece.index, _stand_in(tmp_path, piece.index))
    window._fill_workers(str(tmp_path / "farm"))
    rows = _rows(window.farm_view)
    assert not any("waiting to be joined" in row for row in rows), rows
    assert any("1 failed" in row and "nowhere to write it" in row for row in rows), rows


def test_a_moment_the_nas_refuses_does_not_throw_out_of_the_timer(window, monkeypatch):
    """WinError 1272 came out of the five-second refresh as a traceback in the log."""
    def refused(_self):
        raise OSError(1272, "You can't access this shared folder")
    monkeypatch.setattr(Path, "is_dir", refused)
    assert window._read_share(r"\\192.168.1.6\Transmission\.blueshelf-farm") is None
    window._fill_queue(r"\\192.168.1.6\Transmission\.blueshelf-farm")
