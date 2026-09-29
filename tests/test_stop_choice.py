"""Stopping is the person's choice, wherever the work is being done.

A chunk abandoned at frame 4,900 of 5,000 has thrown away nine minutes; one abandoned at
frame 50 has thrown away nothing. Only the person watching knows which it is, so both the
main run and the helper ask instead of deciding - and the helper used to decide, dropping
the share in hand while its button claimed it was finishing it first.
"""
from __future__ import annotations

import pytest
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from blueshelf import paths
from blueshelf.db import Database
from blueshelf.passthrough_dialog import PassthroughDialog, _Helping


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, monkeypatch, qt_app):
    data = tmp_path / "data"
    (data / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    made = PassthroughDialog(Database(str(data / "library.db")), [])
    yield made
    made.deleteLater()


class _Pretend(_Helping):
    """A helper that is running, and remembers how it was asked to stop."""

    def __init__(self):
        super().__init__("install", None)
        self.asked = None

    def isRunning(self) -> bool:
        return True

    def stop(self, after_this: bool = False) -> None:
        self.asked = "after" if after_this else "now"


@pytest.mark.parametrize("answer, expected", [("after", "after"), ("now", "now")])
def test_the_helper_stops_the_way_it_was_asked_to(window, monkeypatch, answer, expected):
    window.worker = _Pretend()
    monkeypatch.setattr(window, "ask_how_to_stop", lambda _where="": answer)

    window._toggle_help()

    assert window.worker.asked == expected


def test_carrying_on_leaves_the_helper_alone(window, monkeypatch):
    window.worker = _Pretend()
    monkeypatch.setattr(window, "ask_how_to_stop", lambda _where="": "")

    window._toggle_help()

    assert window.worker.asked is None, "answering 'carry on' must not stop anything"
    assert window.help_btn.isEnabled(), "nor leave the button dead"


def test_finishing_the_share_first_does_not_interrupt_it(window, monkeypatch):
    """The distinction that matters: one flag ends the loop, the other kills the work."""
    worker = _Helping("install", None)
    worker.stop(after_this=True)
    assert worker.finish_first and not worker.stop_wanted, "the share in hand is left alone"

    other = _Helping("install", None)
    other.stop()
    assert other.stop_wanted, "and this one drops it"


def test_the_subject_follow_setting_reaches_the_job(window, tmp_path, monkeypatch):
    """Following the subject wipes RVM's memory each time the window moves, which is how a
    performer vanishes mid-shot and vanishes from one eye and not the other. It has to be
    switchable, and the switch has to actually arrive at the job."""
    from blueshelf import passthrough
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(window, "_facts_for", lambda _s: {"width": 7168, "height": 3584,
                                                          "fps": 60.0, "frames": 1000})
    monkeypatch.setattr(passthrough, "guess_projection", lambda *a: "equirectangular")

    window.roi_box.setChecked(False)
    window.jobs.clear()
    assert window._job_for(str(video)).roi is False

    window.roi_box.setChecked(True)
    window.jobs.clear()
    assert window._job_for(str(video)).roi is True


def test_a_model_that_is_not_installed_is_not_offered(window, tmp_path, monkeypatch):
    """MatAnyone2 is an extra most installs do not have, and the published one will not
    even build without a fix. Offering it anyway would fail hours into a job."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import MODELS, NEEDS_EXTRA

    at = next(i for i, (name, _why) in enumerate(MODELS) if name in NEEDS_EXTRA)
    window.install = tmp_path / "vrautomatte"

    monkeypatch.setattr(passthrough, "has_matanyone2", lambda _r: False)
    window._mark_models()
    assert not window.model_box.model().item(at).isEnabled()
    assert "not installed" in window.model_box.itemText(at)

    monkeypatch.setattr(passthrough, "has_matanyone2", lambda _r: True)
    window._mark_models()
    assert window.model_box.model().item(at).isEnabled()
    assert "not installed" not in window.model_box.itemText(at)


def test_choosing_it_and_then_losing_it_falls_back(window, tmp_path, monkeypatch):
    """The choice is remembered between sessions, so it can outlive the install."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import MODELS, NEEDS_EXTRA

    at = next(i for i, (name, _why) in enumerate(MODELS) if name in NEEDS_EXTRA)
    window.install = tmp_path / "vrautomatte"
    monkeypatch.setattr(passthrough, "has_matanyone2", lambda _r: True)
    window._mark_models()
    window.model_box.setCurrentIndex(at)

    monkeypatch.setattr(passthrough, "has_matanyone2", lambda _r: False)
    window._mark_models()

    assert window.model_box.currentData() not in NEEDS_EXTRA, "must not sit on a dead choice"


def _a_queued_job(tmp_path):
    from blueshelf import farm, passthrough
    from blueshelf.passthrough import Job
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "out.mp4"),
              width=320, height=180, fps=30.0, frames=3000)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, passthrough.plan_slices(3000, None, chunk=1000))
    return place, job_id, job


def test_a_video_can_be_taken_out_of_the_shared_queue(window, tmp_path, monkeypatch):
    """Two films queued and only time for one is an ordinary situation, and there was no
    way out of it short of editing the shared folder by hand."""
    place, job_id, _job = _a_queued_job(tmp_path)
    window.help_share_line.setText(str(place.root))
    window._fill_queue(str(place.root))

    rows = [window.queue.item(i) for i in range(window.queue.count())]
    mine = [r for r in rows if r.data(Qt.UserRole) == job_id]
    assert mine, "the row must know which job it is"

    window.queue.setCurrentItem(mine[0])
    window._queue_picked()
    assert window.drop_btn.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window._drop_from_queue()

    assert job_id not in place.jobs(), "gone from the queue"


def test_saying_no_leaves_it_alone(window, tmp_path, monkeypatch):
    place, job_id, _job = _a_queued_job(tmp_path)
    window.help_share_line.setText(str(place.root))
    window._fill_queue(str(place.root))
    mine = next(window.queue.item(i) for i in range(window.queue.count())
                if window.queue.item(i).data(Qt.UserRole) == job_id)
    window.queue.setCurrentItem(mine)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    window._drop_from_queue()

    assert job_id in place.jobs(), "cancelling must not remove anything"


def test_the_finished_chunks_are_not_thrown_away(window, tmp_path, monkeypatch):
    """Hours of matting each. Taking the job out of the queue must not touch them."""
    from blueshelf import passthrough
    place, job_id, job = _a_queued_job(tmp_path)
    folder = passthrough.work_folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    chunk = folder / "part-000.mp4"
    chunk.write_bytes(b"hours of work")

    window.help_share_line.setText(str(place.root))
    window._fill_queue(str(place.root))
    mine = next(window.queue.item(i) for i in range(window.queue.count())
                if window.queue.item(i).data(Qt.UserRole) == job_id)
    window.queue.setCurrentItem(mine)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window._drop_from_queue()

    assert chunk.read_bytes() == b"hours of work"


def test_a_finished_job_whose_owner_has_gone_is_adopted(tmp_path, monkeypatch):
    """Helpers only ever took shares, so a film matted right through would sit there for good
    if the machine that started it had been closed. Everything on disk, nothing moving."""
    import time
    from blueshelf import farm, passthrough
    from blueshelf.passthrough import Job

    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "out.mp4"),
              width=320, height=180, fps=30.0, frames=3000)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    pieces = passthrough.plan_slices(3000, None, chunk=1000)
    job_id = place.post(job, pieces)

    for index, _kind, _tag, path in place._files(job_id):
        path.rename(path.with_name(f"{index}.done"))
    assert place.state(job_id)["complete"]

    assert not place.stranded(job_id), "not while the owner might still be mid-share"

    old = time.time() - farm.ADOPT_WAIT - 60
    for _i, _k, _t, path in place._files(job_id):
        os.utime(path, (old, old))
    assert place.stranded(job_id), "long enough that nobody is coming"

    other = farm.Share(place.root, machine="laptop")
    assert other.take_over(job_id), "the first to ask gets it"
    assert not place.take_over(job_id), "and only the first"


def test_a_job_still_being_matted_is_never_adopted(tmp_path):
    from blueshelf import farm, passthrough
    from blueshelf.passthrough import Job
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "out.mp4"),
              width=320, height=180, fps=30.0, frames=3000)
    place = farm.Share(tmp_path / "farm", machine="desktop")
    job_id = place.post(job, passthrough.plan_slices(3000, None, chunk=1000))

    assert not place.stranded(job_id), "there is still work to hand out"
