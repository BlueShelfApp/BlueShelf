"""Moving a performer's scenes into a folder: what moves, what follows it, and how to undo it."""
from __future__ import annotations

import os

import pytest

from blueshelf import mover, paths
from blueshelf.db import Database
from blueshelf.parser import parse_path


@pytest.fixture()
def library(tmp_path, monkeypatch):
    data, root = tmp_path / "data", tmp_path / "library"
    (data / "thumbs").mkdir(parents=True)
    root.mkdir()
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    db = Database(str(data / "library.db"))
    library_id = db.add_library(str(root))

    def add(name: str, performers: list[str], folder: str = "") -> int:
        path = root / folder / name if folder else root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video data")
        video_id = db.insert_video(library_id, str(path), str(path.relative_to(root)), 10, 1.0,
                                   parse_path(name), True)
        db.set_performers(video_id, performers)
        return video_id

    db.commit()
    yield db, root, add
    db.close()


def performer_id(db, name):
    return next(row["id"] for row in db.performer_counts(True) if row["name"] == name)


def test_a_folder_you_already_have_is_offered(library):
    db, root, add = library
    add("Scene.One.mp4", ["Anna Bell"])
    (root / "anna  bell").mkdir()          # made by hand, spelled loosely
    (root / "sorted" / "Anna Bell").mkdir(parents=True)
    (root / "Someone Else").mkdir()
    found = mover.matching_folders(db, "Anna Bell")
    assert str(root / "anna  bell") in found
    assert str(root / "sorted" / "Anna Bell") in found
    assert str(root / "Someone Else") not in found


def test_the_plan_leaves_alone_what_is_already_there(library):
    db, root, add = library
    add("One.mp4", ["Anna Bell"])
    add("Two.mp4", ["Anna Bell"], folder="Anna Bell")
    destination = str(root / "Anna Bell")
    moves = mover.plan(db, performer_id(db, "Anna Bell"), destination)
    assert [move.name for move in moves] == ["One.mp4"]


def test_scenes_with_other_performers_are_pointed_out_and_can_be_skipped(library):
    db, root, add = library
    add("Solo.mp4", ["Anna Bell"])
    add("Together.mp4", ["Anna Bell", "Kira Perez"])
    destination = str(root / "Anna Bell")
    moves = mover.plan(db, performer_id(db, "Anna Bell"), destination)
    shared = {move.name: move.others for move in moves}
    assert shared["Solo.mp4"] == [] and shared["Together.mp4"] == ["Kira Perez"]
    assert [m.name for m in mover.plan(db, performer_id(db, "Anna Bell"), destination, skip_shared=True)] \
        == ["Solo.mp4"]


def test_moving_takes_the_library_and_the_extras_with_it(library):
    db, root, add = library
    video_id = add("Pool.Day.mp4", ["Anna Bell"])
    (root / "Pool.Day.srt").write_text("subtitles", encoding="utf-8")
    (root / "Pool.Day.jpg").write_bytes(b"artwork")
    destination = str(root / "Anna Bell")

    result = mover.apply(db, mover.plan(db, performer_id(db, "Anna Bell"), destination))
    assert len(result.moved) == 1 and not result.failed

    moved = root / "Anna Bell" / "Pool.Day.mp4"
    assert moved.is_file() and not (root / "Pool.Day.mp4").exists()
    assert (root / "Anna Bell" / "Pool.Day.srt").read_text(encoding="utf-8") == "subtitles"
    assert (root / "Anna Bell" / "Pool.Day.jpg").is_file()

    video = db.get_video(video_id)
    assert video["path"] == str(moved), "the library must follow the file, not lose it"
    assert video["rel_path"] == os.path.join("Anna Bell", "Pool.Day.mp4")


def test_a_name_already_in_the_folder_is_never_written_over(library):
    db, root, add = library
    add("Same.mp4", ["Anna Bell"])
    (root / "Anna Bell").mkdir()
    (root / "Anna Bell" / "Same.mp4").write_bytes(b"something else entirely")

    result = mover.apply(db, mover.plan(db, performer_id(db, "Anna Bell"), str(root / "Anna Bell")))
    assert (root / "Anna Bell" / "Same.mp4").read_bytes() == b"something else entirely"
    assert (root / "Anna Bell" / "Same (2).mp4").read_bytes() == b"video data"
    assert result.moved[0].target.endswith("Same (2).mp4")


def test_a_file_that_vanished_is_reported_not_crashed(library):
    db, root, add = library
    add("Gone.mp4", ["Anna Bell"])
    moves = mover.plan(db, performer_id(db, "Anna Bell"), str(root / "Anna Bell"))
    os.remove(moves[0].source)
    result = mover.apply(db, moves)
    assert not result.moved and len(result.failed) == 1
    assert "no longer there" in result.failed[0][1]


def test_everything_can_be_put_back(library):
    db, root, add = library
    first = add("One.mp4", ["Anna Bell"])
    second = add("Two.mp4", ["Anna Bell"], folder="downloads")
    (root / "downloads" / "Two.srt").write_text("subs", encoding="utf-8")
    destination = str(root / "Anna Bell")

    result = mover.apply(db, mover.plan(db, performer_id(db, "Anna Bell"), destination))
    assert len(result.moved) == 2 and result.log

    back, problems = mover.undo(db, result.log)
    assert back == 2 and not problems
    assert (root / "One.mp4").is_file()
    assert (root / "downloads" / "Two.mp4").is_file()
    assert (root / "downloads" / "Two.srt").read_text(encoding="utf-8") == "subs"
    assert db.get_video(first)["path"] == str(root / "One.mp4")
    assert db.get_video(second)["rel_path"] == os.path.join("downloads", "Two.mp4")
    assert not os.path.exists(result.log), "a batch that was put back is no longer offered"


def test_moving_outside_the_library_still_keeps_the_files(library, tmp_path):
    """Allowed, but the videos leave the library, so the path is kept honest."""
    db, root, add = library
    video_id = add("Away.mp4", ["Anna Bell"])
    outside = tmp_path / "elsewhere"
    result = mover.apply(db, mover.plan(db, performer_id(db, "Anna Bell"), str(outside)))
    assert (outside / "Away.mp4").is_file()
    assert db.get_video(video_id)["path"] == str(outside / "Away.mp4")
    assert mover.library_root(db, str(outside / "Away.mp4")) is None
