"""Choosing videos out of the library by performer, studio, category or folder."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from blueshelf import paths
from blueshelf.db import Database
from blueshelf.parser import parse_path
from blueshelf.pick_dialog import BY_FOLDER, BY_PERFORMER, BY_STUDIO, BY_TAG, PickDialog


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    base = Database(str(data / "library.db"))
    one = base.add_library(str(tmp_path / "one"))
    two = base.add_library(str(tmp_path / "two"))

    def add(name, library_id, performers=(), tags=(), studio="", favorite=0):
        root = tmp_path / ("one" if library_id == one else "two")
        root.mkdir(exist_ok=True)
        path = root / name
        path.write_bytes(b"x")
        vid = base.insert_video(library_id, str(path), name, 1, 1.0, parse_path(name), True)
        base.set_fields(vid, title=name.replace(".mp4", ""), studio=studio,
                          favorite=favorite, hidden=0, duration=600.0,
                          width=7680, height=3840, fps=60.0)
        base.set_performers(vid, list(performers))
        base.set_tags(vid, list(tags))
        return vid, str(path)

    base.added = {
        "anna_one": add("anna one.mp4", one, ["Anna Bell"], ["Passthrough"], "SLR", 1),
        "anna_two": add("anna two.mp4", one, ["Anna Bell"], [], "SLR"),
        "jane": add("jane.mp4", two, ["Jane Doe"], ["Passthrough"], "VirtualTaboo"),
    }
    base.commit()
    yield base
    base.close()


def paths_in(dialog) -> list[str]:
    return [dialog.list.item(row).data(256) for row in range(dialog.list.count())]


def choose(dialog, kind, label_starts: str) -> None:
    """Pick a grouping, then the entry within it, the way a person clicking would."""
    for index in range(dialog.kind.count()):
        if dialog.kind.itemData(index) == (kind, None):
            dialog.kind.setCurrentIndex(index)
            break
    else:
        pytest.fail(f"no {kind} option")
    for index in range(dialog.which.count()):
        if dialog.which.itemText(index).startswith(label_starts):
            dialog.which.setCurrentIndex(index)
            return
    pytest.fail(f"no {label_starts} in the second list")


def test_it_opens_on_everything(qt_app, db):
    dialog = PickDialog(db)
    assert len(paths_in(dialog)) == 3
    dialog.close()


def test_choosing_a_performer_narrows_it(qt_app, db):
    dialog = PickDialog(db)
    choose(dialog, BY_PERFORMER, "Anna Bell")
    assert sorted(p.rsplit("\\", 1)[-1] for p in paths_in(dialog)) \
        == ["anna one.mp4", "anna two.mp4"]
    dialog.close()


def test_choosing_a_category_narrows_it(qt_app, db):
    dialog = PickDialog(db)
    choose(dialog, BY_TAG, "Passthrough")
    assert len(paths_in(dialog)) == 2
    dialog.close()


def test_choosing_a_studio_narrows_it(qt_app, db):
    dialog = PickDialog(db)
    choose(dialog, BY_STUDIO, "VirtualTaboo")
    assert [p.rsplit("\\", 1)[-1] for p in paths_in(dialog)] == ["jane.mp4"]
    dialog.close()


def test_each_folder_is_its_own_choice(qt_app, db):
    dialog = PickDialog(db)
    choose(dialog, BY_FOLDER, "two")
    assert [p.rsplit("\\", 1)[-1] for p in paths_in(dialog)] == ["jane.mp4"]
    dialog.close()


def test_the_usual_views_are_there(qt_app, db):
    dialog = PickDialog(db)
    for index in range(dialog.kind.count()):
        if dialog.kind.itemData(index) == ("view", "favorites"):
            dialog.kind.setCurrentIndex(index)
            break
    assert [p.rsplit("\\", 1)[-1] for p in paths_in(dialog)] == ["anna one.mp4"]
    dialog.close()


def test_searching_filters_the_list(qt_app, db):
    dialog = PickDialog(db)
    dialog.search.setText("jane")
    assert [p.rsplit("\\", 1)[-1] for p in paths_in(dialog)] == ["jane.mp4"]
    dialog.close()


def test_what_is_already_chosen_cannot_be_chosen_twice(qt_app, db):
    already = db.added["jane"][1]
    dialog = PickDialog(db, {already})
    dialog.list.selectAll()
    assert already not in dialog.chosen()
    assert len(dialog.chosen()) == 2, "the other two are still on offer"
    dialog.close()


def test_nothing_is_chosen_until_something_is_selected(qt_app, db):
    dialog = PickDialog(db)
    assert dialog.chosen() == []
    dialog.list.selectAll()
    assert len(dialog.chosen()) == 3
    dialog.close()


# --------------------------------------------------------------------------- the window itself

def test_the_passthrough_window_takes_a_list(qt_app, db, monkeypatch):
    """Several videos at once is the point: the button and the estimate follow the list."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    assert not dialog.start_btn.isEnabled(), "nothing chosen yet"

    dialog._append_source(db.added["anna_one"][1])
    dialog._refresh()
    assert dialog.start_btn.text() == "Make the copy"

    dialog._append_source(db.added["jane"][1])
    dialog._refresh()
    assert dialog.start_btn.text() == "Make 2 copies"
    assert "2 videos" in dialog.estimate.text()

    assert not dialog._append_source(db.added["jane"][1]), "the same file twice is refused"
    dialog.close()


def test_it_sizes_videos_from_the_library_without_probing(qt_app, db, monkeypatch):
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    monkeypatch.setattr(passthrough, "measure",
                        lambda _s: pytest.fail("the library already knew the size"))
    dialog = PassthroughDialog(db)
    dialog._append_source(db.added["anna_one"][1])
    job = dialog._all_jobs()[0]
    assert job.width == 7680 and job.frames == round(600.0 * 60.0)
    dialog.close()


def test_the_timeline_can_be_corrected_by_hand(qt_app):
    """The scan is a guess, so marking a stretch either way has to actually stick."""
    from blueshelf.passthrough import Stretch
    from blueshelf.timeline import Timeline

    strip = Timeline()
    strip.resize(1000, 100)
    strip.show_reading({"times": [0.0], "values": [0.5], "fps": 10.0, "frames": 1000},
                       [Stretch(100, 400)])
    assert strip.matted_share() == pytest.approx(0.301)

    # Drag across the right half and mark it as action.
    strip._from, strip._to = 500.0, 900.0
    strip.mark(True)
    assert strip.matted_share() > 0.3
    assert any(s.start > 400 for s in strip.stretches)

    # Now take the original stretch away again.
    strip._from, strip._to = 0.0, 450.0
    strip.mark(False)
    assert all(s.start > 400 for s in strip.stretches)
    strip.close()


def test_marking_action_over_a_gap_joins_it_up(qt_app):
    from blueshelf.passthrough import Stretch
    from blueshelf.timeline import Timeline

    strip = Timeline()
    strip.resize(1000, 100)
    strip.show_reading({"times": [0.0], "values": [0.5], "fps": 10.0, "frames": 1000},
                       [Stretch(1, 200), Stretch(400, 600)])
    strip._from, strip._to = 150.0, 450.0
    strip.mark(True)
    assert len(strip.stretches) == 1, "the two should have become one"
    assert strip.stretches[0].start == 1 and strip.stretches[0].end == 600
    strip.close()


# --------------------------------------------------------------------------- remembering scans

def a_reading():
    return {"times": [0.0, 5.0, 10.0], "values": [0.1, 0.4, 0.4],
            "exposure": [0.0, 0.8, 0.8], "detector": True, "fps": 30.0, "frames": 900}


def test_a_scan_survives_the_window_closing(qt_app, db, monkeypatch):
    """Reading a video takes minutes; doing it twice for the same file is waste."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    path = db.added["jane"][1]

    first = PassthroughDialog(db)
    first._append_source(path)
    first._scanned(path, a_reading())
    marked = list(first.stretches[path])
    assert marked, "the scan should have found something"
    first.close()

    again = PassthroughDialog(db)
    again._append_source(path)
    assert path in again.readings, "it should not ask for the video to be read again"
    assert [(s.start, s.end) for s in again.stretches[path]] == [(s.start, s.end) for s in marked]
    assert path in again.recalled
    again.close()


def test_hand_corrections_are_kept_too(qt_app, db, monkeypatch):
    """Dragging over the strip is judgement nothing else can reproduce."""
    from blueshelf import passthrough
    from blueshelf.passthrough import Stretch
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    path = db.added["jane"][1]
    first = PassthroughDialog(db)
    first._append_source(path)
    first._scanned(path, a_reading())
    first.stretches[path] = [Stretch(100, 800)]
    first.line_up.stretches = [Stretch(100, 800)]
    first._stretches_edited()
    first.close()

    again = PassthroughDialog(db)
    again._append_source(path)
    assert [(s.start, s.end) for s in again.stretches[path]] == [(100, 800)]
    again.close()


def test_a_changed_file_is_scanned_afresh(qt_app, db, monkeypatch, tmp_path):
    """A re-encode makes the old timings meaningless, and matting against them would be
    worse than not remembering at all."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    path = db.added["jane"][1]
    first = PassthroughDialog(db)
    first._append_source(path)
    first._scanned(path, a_reading())
    first.close()

    from pathlib import Path as _P
    _P(path).write_bytes(b"a completely different video")

    again = PassthroughDialog(db)
    again._append_source(path)
    assert path not in again.readings
    again.close()


def test_a_scan_can_be_thrown_away(qt_app, db, monkeypatch):
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    path = db.added["jane"][1]
    dialog = PassthroughDialog(db)
    dialog._append_source(path)
    dialog._scanned(path, a_reading())
    dialog.sources.setCurrentRow(0)
    dialog._forget()
    assert path not in dialog.readings
    assert db.get_scan(path, *dialog._stamp(path)) is None
    dialog.close()


# --------------------------------------------------------------------------- open alongside

def test_closing_keeps_the_list_for_the_session(qt_app, db, monkeypatch):
    """Closing the window should put it away, not throw away what was queued up in it."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    dialog._append_source(db.added["jane"][1])
    dialog._append_source(db.added["anna_one"][1])
    dialog.close()
    assert dialog.isHidden(), "it should be out of the way"
    assert dialog.sources.count() == 2, "and still hold what was put in it"
    dialog.shut_down()


def test_videos_dragged_from_the_library_are_added(qt_app, db, monkeypatch):
    from PySide6.QtCore import QMimeData
    from blueshelf import passthrough
    from blueshelf.library import VIDEO_MIME
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    carried = QMimeData()
    both = [db.added["jane"][1], db.added["anna_one"][1]]
    carried.setData(VIDEO_MIME, "\n".join(both).encode("utf-8"))
    assert dialog._dropped_paths(carried) == both
    dialog.shut_down()


def test_files_dragged_off_the_desktop_are_added(qt_app, db, monkeypatch, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    film, note = tmp_path / "scene.mp4", tmp_path / "notes.txt"
    film.write_bytes(b"x")
    note.write_bytes(b"x")
    carried = QMimeData()
    carried.setUrls([QUrl.fromLocalFile(str(film)), QUrl.fromLocalFile(str(note))])
    found = dialog._dropped_paths(carried)
    assert len(found) == 1 and found[0].endswith("scene.mp4"), "only videos, not the text file"
    dialog.shut_down()


def test_the_library_offers_its_videos_for_dragging(qt_app, db):
    """Dragged out as a BlueShelf-only type, never as file URLs - dropping a scene on a
    folder window would otherwise start copying tens of gigabytes."""
    from PySide6.QtCore import Qt
    from blueshelf.library import VIDEO_MIME, VideoModel

    model = VideoModel()
    model.set_videos(db.list_videos())
    assert model.mimeTypes() == [VIDEO_MIME]
    index = model.index(0, 0)
    assert model.flags(index) & Qt.ItemIsDragEnabled
    carried = model.mimeData([index, model.index(0, 1)])
    assert not carried.hasUrls()
    lines = bytes(carried.data(VIDEO_MIME)).decode().splitlines()
    assert len(lines) == 1, "the same video picked twice is still one video"


def test_the_shares_spinner_is_hidden_once_chunking_decides(qt_app, db, monkeypatch):
    """Chunk size overrides it, so offering it would be offering a control that does nothing."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    dialog.spread.setChecked(True)
    dialog.chunk_box.setValue(5000)
    dialog._refresh()
    assert not dialog.parts_row.isVisibleTo(dialog.spread_box)
    dialog.chunk_box.setValue(0)
    dialog._refresh()
    assert dialog.parts_row.isVisibleTo(dialog.spread_box)
    dialog.shut_down()


def test_the_machine_panel_reports_who_is_working(qt_app, db, monkeypatch, tmp_path):
    from blueshelf import farm, passthrough
    from blueshelf.passthrough import Job
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    place = farm.Share(tmp_path / "farm", machine="laptop")
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=6000)
    place.post(job, passthrough.split(6000, 1, 0, chunk=2000))

    dialog = PassthroughDialog(db)
    dialog._fill_workers(str(tmp_path / "farm"))
    said = [dialog.farm_view.item(n).text() for n in range(dialog.farm_view.count())]
    assert any("No machine is working" in line for line in said), said

    place.claim()
    dialog._fill_workers(str(tmp_path / "farm"))
    said = [dialog.farm_view.item(n).text() for n in range(dialog.farm_view.count())]
    assert any("laptop" in line and "chunk 1" in line for line in said), said
    dialog.shut_down()


def test_failed_chunks_can_be_put_back_by_hand(qt_app, db, monkeypatch, tmp_path):
    """With two machines going, you want to retry what failed without restarting the job."""
    from blueshelf import farm, passthrough
    from blueshelf.passthrough import Job
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    root = tmp_path / "farm"
    place = farm.Share(root, machine="helper")
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=4000)
    job_id = place.post(job, passthrough.split(4000, 1, 0, chunk=1000))
    for _ in range(2):
        _i, piece, _j = place.claim()
        place.fail(job_id, piece.index, "nowhere to write it")

    dialog = PassthroughDialog(db)
    dialog.share_line.setText(str(root))
    dialog._fill_workers(str(root))
    assert dialog.retry_btn.isEnabled(), "a failure should offer the button"
    said = [dialog.farm_view.item(n).text() for n in range(dialog.farm_view.count())]
    assert any("failed" in line for line in said), said

    dialog._retry_failed()
    state = place.state(job_id)
    assert state["failed"] == 0 and state["waiting"] == 4
    assert not dialog.retry_btn.isEnabled(), "nothing left to retry"
    dialog.shut_down()


def test_a_part_way_copy_is_offered_when_chunks_exist(qt_app, db, monkeypatch, tmp_path):
    from blueshelf import passthrough
    from blueshelf.passthrough import Job
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    dialog = PassthroughDialog(db)
    # The button needs somewhere to run the work as well as something to build from.
    dialog.install = tmp_path
    monkeypatch.setattr(dialog, "_facts_for",
                        lambda _s: {"width": 320, "height": 180, "fps": 30.0, "frames": 4000})
    dialog._append_source(str(video))
    dialog.chunk_box.setValue(1000)
    dialog._refresh()
    assert not dialog.sofar_btn.isEnabled(), "nothing matted yet"

    job = dialog._job_for(str(video))
    folder = passthrough.work_folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    for piece in dialog._plan_for(job)[:2]:
        passthrough.blank_segment(job, piece.kept, passthrough.part_file(folder, piece))
    dialog._refresh()
    assert dialog.sofar_btn.isEnabled()
    assert "2 that are done" in dialog.sofar_btn.text()
    dialog.shut_down()


def test_stopping_offers_a_choice(qt_app, db, monkeypatch):
    """Whether to finish the chunk in hand is the user's call, not a rule."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import _Batch

    worker = _Batch(None, [])
    assert not worker.stop_wanted and not worker.finish_first

    worker.stop(after_this=True)
    assert worker.finish_first and not worker.stop_wanted, "the chunk in hand carries on"

    other = _Batch(None, [])
    other.stop()
    assert other.stop_wanted, "dropping it stops the matting straight away"


def test_settings_do_not_change_when_the_window_is_scrolled(qt_app, db, monkeypatch):
    """Five notches of a mouse wheel over the lens angle turned 180 into 155 and pointed a
    half-finished job at an empty folder."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QWheelEvent
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    dialog.fov_box.setValue(180)
    dialog.fov_box.clearFocus()

    def scroll(widget, notches=5):
        for _ in range(notches):
            qt_app.sendEvent(widget, QWheelEvent(
                QPoint(5, 5), widget.mapToGlobal(QPoint(5, 5)), QPoint(0, 0),
                QPoint(0, -120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))

    scroll(dialog.fov_box)
    assert dialog.fov_box.value() == 180, "scrolling past it must change nothing"

    before = dialog.chunk_box.value()
    scroll(dialog.chunk_box)
    assert dialog.chunk_box.value() == before
    dialog.shut_down()


def test_the_machine_check_is_reachable_from_the_helper_tab(qt_app, db, monkeypatch, tmp_path):
    """A helper never ticks "spread the work", so a check hidden inside that box is a check
    on the one machine that does not need it."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    dialog.tabs.setCurrentIndex(1)
    assert dialog.help_bench_btn.isVisibleTo(dialog.tabs.widget(1))
    assert not dialog.spread.isChecked(), "the helper tab needs nothing ticked"
    dialog.shut_down()


def test_the_check_uses_the_folder_on_the_tab_in_front(qt_app, db, monkeypatch, tmp_path):
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    helper_folder = tmp_path / "from-the-helper-tab"
    helper_folder.mkdir()
    dialog = PassthroughDialog(db)
    dialog.share_line.setText(str(tmp_path / "from-the-other-tab"))
    dialog.help_share_line.setText(str(helper_folder))
    dialog.tabs.setCurrentIndex(1)

    seen = {}
    real = dialog._read_share

    def watching(root):
        seen["root"] = root
        return real(root)

    class NotReally:
        """Stands in for the check itself; only which folder it was given matters here."""

        def __init__(self, *a, **k):
            self.line = self.step = self.ended = _Nothing()

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(dialog, "_read_share", watching)
    monkeypatch.setattr("blueshelf.passthrough_dialog._Bench", NotReally)
    dialog._check_machine()
    assert seen["root"] == str(helper_folder)
    dialog.shut_down()


class _Nothing:
    def connect(self, *a, **k):
        pass


def test_a_helper_times_its_card_against_the_waiting_job(qt_app, db, monkeypatch, tmp_path):
    """A helper has no list of videos, so without this it would silently time only the link
    - and the card is the half that decides how much work it can take."""
    from blueshelf import farm, passthrough
    from blueshelf.passthrough import Job
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    place = farm.Share(tmp_path / "farm", machine="coordinator")
    job = Job(source=str(video), output=str(tmp_path / "scene_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=4000)
    place.post(job, passthrough.split(4000, 1, 0, chunk=1000))

    dialog = PassthroughDialog(db)
    assert dialog._current_source() == "", "a helper has nothing on its list"
    found = dialog._job_from_the_queue(place)
    assert found is not None and found.source == str(video)
    dialog.shut_down()


def test_a_job_whose_video_is_unreachable_is_not_used_for_timing(qt_app, db, monkeypatch,
                                                                 tmp_path):
    """No point timing against a file this machine cannot open - and if it cannot, that is
    the real problem, not the speed."""
    from blueshelf import farm, passthrough
    from blueshelf.passthrough import Job
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    place = farm.Share(tmp_path / "farm", machine="coordinator")
    job = Job(source=str(tmp_path / "nowhere.mp4"),
              output=str(tmp_path / "nowhere_FISHEYE180_alpha.mp4"),
              width=320, height=180, fps=30.0, frames=4000)
    place.post(job, passthrough.split(4000, 1, 0, chunk=1000))

    dialog = PassthroughDialog(db)
    assert dialog._job_from_the_queue(place) is None
    dialog.shut_down()


def test_the_passthrough_window_can_ask_for_the_library(qt_app, db, monkeypatch):
    """A long matte may run for hours; the library need not sit there while it does."""
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    asked = []
    dialog.wants_library.connect(asked.append)
    dialog.library_action.setChecked(False)
    dialog.library_action.setChecked(True)
    assert asked == [False, True]
    dialog.shut_down()


def test_closing_the_passthrough_window_says_so(qt_app, db, monkeypatch):
    from blueshelf import passthrough
    from blueshelf.passthrough_dialog import PassthroughDialog

    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    dialog = PassthroughDialog(db)
    told = []
    dialog.shown.connect(told.append)
    dialog.close()
    assert told == [False], "so the menu tick can follow it"
    assert dialog.isHidden()
    dialog.shut_down()
