"""The window somebody uses to bring their old library in."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from blueshelf import importing, paths, sidecars
from blueshelf.db import Database
from blueshelf.import_dialog import ImportDialog
from blueshelf.parser import parse_path
from test_importing import _stash


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def ready(tmp_path, monkeypatch, qt_app):
    """Their Stash database, and our films in a different place from where Stash saw them."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(importing, "likely_places", lambda: [])
    ours = tmp_path / "films" / "WankzVR"
    ours.mkdir(parents=True)
    films = []
    for name, filler in (("pool day.mp4", b"one"), ("shower.mp4", b"two")):
        film = ours / name
        film.write_bytes(filler * 400)
        films.append(film)
    db = Database(str(tmp_path / "library.db"))
    library_id = db.add_library(str(tmp_path / "films"))
    ids = [db.insert_video(library_id, str(f), f.name, f.stat().st_size, 1.0, parse_path(f.name), True)
           for f in films]
    db.commit()
    stash = _stash(tmp_path / "stash-go.sqlite")
    yield db, ids, films, stash
    db.close()


def wait_for(qt_app, done, seconds=60):
    deadline = time.time() + seconds
    while not done() and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.05)
    return done()


def test_it_shows_what_would_happen_before_anything_does(ready, qt_app, monkeypatch):
    db, ids, films, stash = ready
    dialog = ImportDialog(db)
    try:
        dialog.where.setText(str(stash))
        dialog._look()
        assert wait_for(qt_app, lambda: dialog.plan is not None), dialog.stage.text()
        # Their paths are D:\videos\WankzVR; ours are somewhere else entirely. The names and
        # sizes do not match either, because Stash recorded different sizes.
        assert "0 of 2 films matched" in dialog.summary.text(), dialog.summary.text()
        assert dialog.missing.count() == 2
        assert not dialog.bring_btn.isEnabled(), "nothing to bring in yet"
        assert db.get_video(ids[0])["studio"] == "", "and nothing has been changed"

        # It works out where their folder is ours and offers it, already ticked.
        assert dialog.folders.count() >= 1
        offered = dialog.folders.item(0)
        assert offered.checkState() == Qt.Checked
        theirs, ours = offered.data(Qt.UserRole)
        assert theirs.lower().startswith("d:\\videos")

        dialog._look()
        assert wait_for(qt_app, lambda: dialog.plan is not None and dialog.plan.matched)
        assert "2 of 2 films matched" in dialog.summary.text()
        assert dialog.bring_btn.isEnabled()

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        dialog._bring()
        assert wait_for(qt_app, lambda: "Done." in dialog.stage.text()), dialog.stage.text()
        video = db.get_video(ids[0])
        assert video["studio"] == "WankzVR" and video["performers"] == ["Anna Bell", "Gabbie Carter"]
        assert len(db.scenes(ids[0])) == 2
        assert films[0].read_bytes() == b"one" * 400, "their film is untouched"
    finally:
        dialog.shut_down()
        dialog.deleteLater()


def test_something_that_is_not_a_library_says_so_plainly(ready, qt_app, monkeypatch, tmp_path):
    db, _ids, _films, _stash = ready
    said = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, message, *a, **k: said.append(message))
    dialog = ImportDialog(db)
    try:
        dialog.where.setText(str(tmp_path / "there-is-nothing-here.sqlite"))
        dialog._look()
        assert wait_for(qt_app, lambda: bool(said)), "it should have complained"
        assert "no database" in said[0].lower() or "there is no" in said[0].lower(), said
        assert not dialog.bring_btn.isEnabled()
    finally:
        dialog.shut_down()
        dialog.deleteLater()


def test_nothing_chosen_is_not_an_error(ready, qt_app):
    db, _ids, _films, _stash = ready
    dialog = ImportDialog(db)
    try:
        dialog.where.clear()
        dialog._look()
        assert "Choose where" in dialog.stage.text()
    finally:
        dialog.shut_down()
        dialog.deleteLater()


def test_the_menu_can_write_the_library_out_and_take_it_back(ready, qt_app, monkeypatch):
    """The other direction: what BlueShelf knows, in files beside the films."""
    db, ids, films, _stash = ready
    db.set_fields(ids[0], title="Pool Day", rating=4)
    db.commit()
    assert sidecars.write_all(db)["written"] == 2
    beside = films[0].with_suffix(".nfo")
    assert beside.is_file() and "Pool Day" in beside.read_text(encoding="utf-8")
    assert sidecars.remove_all(db) == 2 and not beside.exists()
