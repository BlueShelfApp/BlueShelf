"""The DLNA part of the sharing window: switching it on and off, renaming, and a taken port."""
from __future__ import annotations

import socket

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from blueshelf import dlna, firewall, paths
from blueshelf.db import Database
from blueshelf.remote_dialog import DlnaBox


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def box(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "db_path", lambda: tmp_path / "library.db")
    monkeypatch.setattr(firewall, "dlna_rules_exist", lambda port: True)  # never touch the real firewall
    db = Database(str(tmp_path / "library.db"))
    # announce=False keeps the tests off the real network; the start routine is what is under test.
    server = dlna.DlnaServer(name="Before", udn="u-1", port=0, ssdp_port=41950)
    monkeypatch.setattr(server, "start", lambda announce=True, _real=server.start: _real(announce=False))
    widget = DlnaBox(db, server)
    yield widget, db, server
    server.stop()
    db.close()


def test_switching_on_starts_sharing_and_remembers_it(box):
    widget, db, server = box
    assert not server.running and "Switched off" in widget.status.text()
    widget.check.setChecked(True)
    assert server.running
    assert db.get_setting("dlna_enabled") is True or db.get_setting("dlna_enabled") in ("1", 1, "True")
    assert int(db.get_setting("dlna_port")) == server.port, "the port is kept so the firewall rule stays valid"
    widget.check.setChecked(False)
    assert not server.running


def test_a_new_name_is_used_straight_away(box):
    widget, db, server = box
    widget.check.setChecked(True)
    widget.name_edit.setText("Living room")
    widget._rename()
    assert server.name == "Living room" and server.running
    assert db.get_setting("dlna_name") == "Living room"
    widget.name_edit.setText("   ")
    widget._rename()
    assert widget.name_edit.text() == "Living room", "a blank name is refused, not saved"


def test_a_taken_port_falls_back_to_a_free_one(box, monkeypatch):
    widget, db, server = box
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    server.port = taken
    try:
        widget.check.setChecked(True)
        assert server.running and server.port != taken
        assert int(db.get_setting("dlna_port")) == server.port
    finally:
        blocker.close()


def test_when_nothing_will_start_the_switch_goes_back_off(box, monkeypatch):
    widget, db, server = box
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: shown.append(args))
    monkeypatch.setattr(dlna, "start_saved", lambda *_: "Address already in use")
    widget.check.setChecked(True)
    assert not widget.check.isChecked()
    assert db.get_setting("dlna_enabled") in (False, "0", 0, "False")
    assert shown, "the person should be told why"
