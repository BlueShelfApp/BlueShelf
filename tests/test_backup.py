import os
import time

import pytest

from blueshelf import backup, paths
from blueshelf.db import Database
from blueshelf.parser import parse_path

SCENE = "Site.24.03.05.Anna.Bell.Pool.Day.XXX.mp4"


@pytest.fixture()
def library(tmp_path, monkeypatch):
    data = tmp_path / "data"
    thumbs = data / "thumbs"
    thumbs.mkdir(parents=True)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "thumbs_dir", lambda: thumbs)
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")
    db = Database(str(data / "library.db"))
    lib = db.add_library(str(tmp_path / "videos"))
    video_id = db.insert_video(lib, str(tmp_path / "videos" / SCENE), SCENE, 10, 1.0, parse_path(SCENE), True)
    db.set_fields(video_id, rating=4, favorite=1)
    (thumbs / f"{video_id}.jpg").write_bytes(b"thumb")
    yield db, data, thumbs, video_id
    db.close()


def test_backup_records_what_it_holds(library, tmp_path):
    _db, _data, _thumbs, _video_id = library
    out = str(tmp_path / backup.default_name())
    summary = backup.create(out)
    assert os.path.isfile(out)
    assert summary["videos"] == 1 and summary["performers"] == 1 and summary["thumbs"] == 1
    assert backup.inspect(out)["videos"] == 1


def test_backup_works_while_the_app_has_the_database_open(library, tmp_path):
    db, _data, _thumbs, video_id = library
    db.set_fields(video_id, title="Still Writing")  # connection stays open throughout
    out = str(tmp_path / backup.default_name())
    assert backup.create(out)["videos"] == 1


def test_restore_brings_back_edits_and_thumbnails(library, tmp_path):
    db, data, thumbs, video_id = library
    out = str(tmp_path / backup.default_name())
    backup.create(out)
    db.set_fields(video_id, rating=1, favorite=0, title="Changed After Backup")
    (thumbs / f"{video_id}.jpg").unlink()
    db.close()

    details = backup.restore(out)
    assert os.path.isdir(details["replaced_data_kept_in"])
    restored = Database(str(data / "library.db"))
    video = restored.get_video(video_id)
    assert video["rating"] == 4 and video["favorite"] == 1 and video["title"] == "Pool Day"
    assert (thumbs / f"{video_id}.jpg").read_bytes() == b"thumb"
    restored.close()


def test_restoring_something_that_is_not_a_backup(tmp_path, library):
    junk = tmp_path / "notes.zip"
    junk.write_bytes(b"not a zip at all")
    assert backup.inspect(str(junk)) is None
    with pytest.raises(ValueError):
        backup.restore(str(junk))


def test_automatic_backups_are_spaced_out_and_pruned(library, monkeypatch):
    first = backup.run_automatic(every_days=7)
    assert first is not None
    assert backup.run_automatic(every_days=7) is None, "should not back up again so soon"

    folder = backup.automatic_dir()
    for n in range(backup.KEEP_AUTOMATIC + 2):
        backup.create(os.path.join(folder, f"blueshelf-old{n}{backup.SUFFIX}"))
    old = time.time() - 900000  # age every backup, including the one just made
    for name in os.listdir(folder):
        os.utime(os.path.join(folder, name), (old, old))
    assert backup.run_automatic(every_days=7) is not None
    kept = [n for n in os.listdir(folder) if n.endswith(backup.SUFFIX)]
    assert len(kept) == backup.KEEP_AUTOMATIC
