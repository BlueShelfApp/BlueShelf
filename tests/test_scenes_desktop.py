"""Scenes on the desktop: marked in the player, chosen and ordered on the From scenes tab."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from blueshelf import passthrough, paths
from blueshelf.db import Database
from blueshelf.parser import parse_path
from blueshelf.scenes_tab import ScenesTab, default_name, read_clock
from test_scenes import SCENE, _film, _red_at


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def marked(tmp_path, qt_app):
    film = _film(tmp_path / SCENE)
    db = Database(str(tmp_path / "library.db"))
    library = db.add_library(str(tmp_path))
    video = db.insert_video(library, str(film), SCENE, film.stat().st_size, 1.0, parse_path(SCENE), True)
    for start, end in ((2, 5), (8, 11), (14, 17)):
        db.add_scene(video, start, end)
    yield db, video, film
    db.close()


def wait_for(qt_app, done, seconds=60):
    deadline = time.time() + seconds
    while not done() and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.05)
    return done()


def test_times_are_read_the_way_people_write_them():
    assert read_clock("12:30") == 750 and read_clock("1:02:03") == 3723
    assert read_clock("12:30.5") == 750.5 and read_clock(" 95 ") == 95
    assert read_clock("half past") is None and read_clock("") is None
    assert default_name([3]) == "scene 3" and default_name([3, 1]) == "scenes 3+1"


def test_a_films_scenes_are_offered_ticked_in_film_order(marked):
    db, video, _film_path = marked
    tab = ScenesTab(db)
    assert tab.films.count() == 1 and tab.films.currentData() == video
    assert [s["number"] for s in tab.chosen()] == [1, 2, 3]
    assert "3 scenes chosen, 0:09 altogether" in tab.total.text()
    assert tab.name_box.text() == "scenes 1+2+3"


def test_they_can_be_put_in_another_order_and_left_out(marked):
    db, _video, _film_path = marked
    tab = ScenesTab(db)
    tab.list.setCurrentRow(2)
    tab._move(-1)
    tab._move(-1)                                   # scene 3 to the top
    tab.list.item(2).setCheckState(Qt.Unchecked)   # and scene 2 left out
    assert [s["number"] for s in tab.chosen()] == [3, 1]
    assert tab.name_box.text() == "scenes 3+1"
    tab.name_box.setText("the best")
    tab._named = True
    tab._choose_all(True)
    assert tab.name_box.text() == "the best", "a name typed in is not replaced"


def test_a_scene_can_be_trimmed_and_renamed_without_losing_the_order(marked):
    db, video, _film_path = marked
    tab = ScenesTab(db)
    tab.list.setCurrentRow(2)
    tab._move(-2)                                  # 3, 1, 2
    tab.list.setCurrentRow(0)
    tab.start_box.setText("0:13")
    tab.end_box.setText("0:18")
    tab.label_box.setText("Finale")
    tab._save_scene()
    assert [s["label"] for s in tab.chosen()] == ["Finale", "Scene 1", "Scene 2"]
    kept = {s["label"]: (s["start"], s["end"]) for s in db.scenes(video)}
    assert kept["Finale"] == (13, 18)


def test_the_chosen_scenes_are_cut_in_order_and_handed_on(marked, qt_app):
    db, _video, film = marked
    tab = ScenesTab(db)
    tab.list.setCurrentRow(2)
    tab._move(-2)                                  # scene 3 (14-17 s) first, then 1 and 2
    tab.list.item(2).setCheckState(Qt.Unchecked)
    handed = []
    tab.ready.connect(handed.append)
    tab._make()
    assert wait_for(qt_app, lambda: bool(handed) and tab.cutting is None), tab.stage.text()
    clip = passthrough.Path(handed[0])
    assert clip.parent == passthrough.scenes_folder(film) and clip.name.endswith("scenes 3+1.mp4")
    assert 150 <= _red_at(clip, 0.05) <= 175, "it opens on scene 3, from 14 s"
    assert "Make copies" in tab.stage.text()

    tab._make()                                    # again: a new clip, never over the first
    assert wait_for(qt_app, lambda: len(handed) == 2 and tab.cutting is None)
    assert handed[1] != handed[0] and handed[1].endswith("(2).mp4")


def test_the_passthrough_window_puts_the_clip_on_its_list(tmp_path, monkeypatch, qt_app):
    from blueshelf.passthrough_dialog import PassthroughDialog
    data = tmp_path / "data"
    (data / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    monkeypatch.setattr(passthrough, "find_install", lambda _saved=None: None)
    window = PassthroughDialog(Database(str(data / "library.db")), [])
    try:
        assert window.tabs.tabText(2) == "From scenes" and window.tabs.tabText(1) == "Help another computer"
        clip = _film(tmp_path / "clip.mp4", seconds=2)
        window.tabs.setCurrentIndex(2)
        window.scenes_tab.ready.emit(str(clip))
        assert str(clip) in window._paths()
        assert window.tabs.currentIndex() == 0, "back to where it will be made"
    finally:
        window.shut_down()
        window.deleteLater()


class _Mpv:
    """Just enough of mpv for marking: where the film is, and somewhere to say things."""

    def __init__(self):
        self.time_pos = 0.0
        self.said = []

    def show_text(self, text, _ms=0):
        self.said.append(text)


def test_the_desktop_player_marks_a_scene_with_two_presses(marked, qt_app):
    from blueshelf.player import PlayerWidget
    db, video, _film_path = marked
    player = PlayerWidget(db)
    try:
        player.mpv = _Mpv()
        player.video = db.get_video(video)
        player.seek.set_duration(20)
        player.mpv.time_pos = 3.0
        player.mark_scene()
        assert player.marking == 3.0 and player.mark_btn.text() == "End scene · 0:03"
        player.mpv.time_pos = 9.5
        player.mark_scene()
        assert player.marking is None and player.mark_btn.text() == "Mark scene"
        assert (3.0, 9.5) in [(s["start"], s["end"]) for s in db.scenes(video)]
        assert "Scene kept" in player.mpv.said[-1]
        assert len(player.seek.scenes) == 4, "the bar shows every scene of this film"
        player.seek.repaint()                      # drawing the bands must not fail

        player.mpv.time_pos = 12.0
        player.mark_scene()
        player.mpv.time_pos = 12.5
        player.mark_scene()
        assert "Not kept" in player.mpv.said[-1], "too short to be meant"
    finally:
        player.mpv = None
        player.deleteLater()
