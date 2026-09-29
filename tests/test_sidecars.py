"""Writing a library out beside the films, so nothing organised in BlueShelf is trapped in it."""
from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from blueshelf import sidecars
from blueshelf.db import Database
from blueshelf.importing import Match, apply_plan, read_nfos
from blueshelf.parser import parse_path


@pytest.fixture()
def organised(tmp_path):
    """One film, organised the way somebody would: named, rated, tagged, with a scene marked."""
    folder = tmp_path / "films"
    folder.mkdir()
    film = folder / "WankzVR.24.03.05.Anna.Bell.Pool.Day.XXX.mp4"
    film.write_bytes(b"x" * 500)
    db = Database(str(tmp_path / "library.db"))
    library_id = db.add_library(str(folder))
    video_id = db.insert_video(library_id, str(film), film.name, film.stat().st_size, 1.0,
                               parse_path(film.name), True)
    db.set_fields(video_id, title="Pool Day", studio="WankzVR", release_date="2024-03-05",
                  details="A day at the pool.", url="https://example.test/pool", rating=4,
                  favorite=1, play_count=3, last_played=1_789_927_451.0, resume_pos=930.5,
                  duration=3600.0, is_vr=1, vr_layout=1, vr_proj=2, vr_lens_fov=190)
    db.set_performers(video_id, ["Anna Bell", "Gabbie Carter"])
    db.set_tags(video_id, ["POV", "Blonde"])
    db.add_scene(video_id, 120, 260, "The pool")
    db.commit()
    yield db, video_id, film, folder
    db.close()


def test_everything_about_a_film_is_written_beside_it(organised):
    db, video_id, film, _folder = organised
    beside = sidecars.write_one(db, video_id)
    assert beside == film.with_suffix(".nfo") and beside.is_file()
    root = ElementTree.parse(beside).getroot()
    assert root.findtext("title") == "Pool Day"
    assert root.findtext("studio") == "WankzVR" and root.findtext("premiered") == "2024-03-05"
    assert root.findtext("plot") == "A day at the pool."
    assert root.findtext("userrating") == "8", "four of BlueShelf's stars are eight of Kodi's ten"
    assert root.findtext("playcount") == "3" and root.findtext("isfavorite") == "true"
    assert root.findtext("resume/position") == "930.5"
    assert sorted(t.text for t in root.findall("tag")) == ["Blonde", "POV"]
    assert [a.findtext("name") for a in root.findall("actor")] == ["Anna Bell", "Gabbie Carter"]
    scene = root.find("scene")
    assert scene.text == "The pool" and scene.get("start") == "120.000" and scene.get("end") == "260.000"
    assert root.find("vr").get("fov") == "190.0"
    assert film.read_bytes() == b"x" * 500, "the film itself is never touched"


def test_it_comes_back_after_everything_here_is_lost(organised, tmp_path):
    """The point of writing them: a new library, the same folder, and nothing retyped."""
    db, video_id, film, folder = organised
    sidecars.write_one(db, video_id)
    was = db.get_video(video_id)

    fresh = Database(str(tmp_path / "again.db"))
    try:
        library_id = fresh.add_library(str(folder))
        again = fresh.insert_video(library_id, str(film), film.name, film.stat().st_size, 1.0,
                                   parse_path(film.name), True)
        fresh.commit()
        apply_plan(fresh, Match(fresh).plan(read_nfos([folder])))
        now = fresh.get_video(again)
        assert now["title"] == was["title"] and now["studio"] == was["studio"]
        assert now["release_date"] == was["release_date"] and now["details"] == was["details"]
        assert now["rating"] == was["rating"] and now["play_count"] == was["play_count"]
        assert now["performers"] == was["performers"] and sorted(now["tags"]) == sorted(was["tags"])
        assert now["resume_pos"] == was["resume_pos"] and now["favorite"] == was["favorite"]
        assert [(s["start"], s["end"], s["label"]) for s in fresh.scenes(again)] == [(120.0, 260.0, "The pool")]
    finally:
        fresh.close()


def test_writing_again_does_not_touch_a_file_that_would_not_change(organised):
    db, video_id, film, _folder = organised
    beside = sidecars.write_one(db, video_id)
    stamp = beside.stat().st_mtime_ns
    sidecars.write_one(db, video_id)
    assert beside.stat().st_mtime_ns == stamp, "nothing changed, so nothing was written"
    db.set_fields(video_id, rating=5)
    sidecars.write_one(db, video_id)
    assert ElementTree.parse(beside).getroot().findtext("userrating") == "10"


def test_they_are_only_written_when_asked_for(organised):
    db, video_id, film, _folder = organised
    sidecars.keep_up_to_date(db, video_id)
    assert not film.with_suffix(".nfo").exists(), "off unless the person turns it on"
    db.set_setting(sidecars.WRITE_SETTING, True)
    sidecars.keep_up_to_date(db, video_id)
    assert film.with_suffix(".nfo").is_file()


def test_all_of_them_at_once_and_taking_them_away_again(organised, tmp_path):
    db, _video_id, film, folder = organised
    other = folder / "another.mp4"
    other.write_bytes(b"y")
    db.insert_video(db.libraries()[0]["id"], str(other), other.name, 1, 1.0, parse_path(other.name), True)
    db.commit()
    assert sidecars.write_all(db)["written"] == 2

    # Somebody else's file of the same name is left alone.
    theirs = folder / "not-ours.nfo"
    theirs.write_text("<movie><title>Theirs</title></movie>", encoding="utf-8")
    assert sidecars.remove_all(db) == 2
    assert not film.with_suffix(".nfo").exists() and theirs.is_file()


def test_a_film_on_a_disc_that_cannot_be_written_to_is_not_fatal(organised, monkeypatch):
    db, video_id, _film, _folder = organised

    def refuse(*_args, **_named):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", refuse)
    assert sidecars.write_one(db, video_id) is None
    assert sidecars.write_all(db)["could not"] >= 1
