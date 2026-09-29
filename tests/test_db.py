import os

from blueshelf.db import Database
from blueshelf.parser import parse_path, performer_key

SCENE = "Site.24.03.05.Anna.Bell.Pool.Day.XXX.mp4"
MOVIE = "Some.Movie.2024.1080p.WEB-DL.x264-GRP.mkv"


def make_db(tmp_path):
    db = Database(str(tmp_path / "library.db"))
    return db, db.add_library(str(tmp_path))


def insert(db, lib, tmp_path, name, size=100):
    return db.insert_video(lib, str(tmp_path / name), name, size, 1.0, parse_path(name), True)


def test_listing_filters(tmp_path):
    db, lib = make_db(tmp_path)
    scene = insert(db, lib, tmp_path, SCENE)
    movie = insert(db, lib, tmp_path, MOVIE)
    assert [v["id"] for v in db.list_videos()] == [scene]
    assert {v["id"] for v in db.list_videos(show_hidden=True)} == {scene, movie}
    performer_id = db.known_performers()[performer_key("Anna Bell")][0]
    assert [v["id"] for v in db.list_videos(performer_id=performer_id)] == [scene]
    assert [v["id"] for v in db.list_videos(search="anna pool")] == [scene]
    assert db.list_videos(search="nobody") == []
    assert db.view_counts(False)["hidden"] == 1
    assert db.list_videos(video_id=movie, show_hidden=True)[0]["id"] == movie


def test_manual_edits_survive_rescan_but_not_forced_reparse(tmp_path):
    db, lib = make_db(tmp_path)
    vid = insert(db, lib, tmp_path, SCENE)
    db.set_performers(vid, ["Cara Mell"])
    db.set_fields(vid, title="Better Title", meta_locked=1)
    db.apply_parsed(vid, parse_path(SCENE), True)
    video = db.get_video(vid)
    assert video["performers"] == ["Cara Mell"]
    assert video["title"] == "Better Title"
    db.apply_parsed(vid, parse_path(SCENE), True, force=True)
    video = db.get_video(vid)
    assert video["performers"] == ["Anna Bell"]
    assert video["title"] == "Pool Day"


def test_rename_performer_merges_spelling_variants(tmp_path):
    db, lib = make_db(tmp_path)
    insert(db, lib, tmp_path, "Site.24.03.05.Kira.Prez.XXX.mp4")
    insert(db, lib, tmp_path, "Site.24.03.06.Kira.Perez.XXX.mp4")
    db.rename_performer(db.known_performers()["kira prez"][0], "Kira Perez")
    counts = db.performer_counts(True)
    assert [(r["name"], r["n"]) for r in counts] == [("Kira Perez", 2)]


def test_studio_variants_are_grouped(tmp_path):
    db, lib = make_db(tmp_path)
    insert(db, lib, tmp_path, "brazzersexxtra.24.03.05.anna.bell.xxx.mp4")
    insert(db, lib, tmp_path, "BrazzersExxtra.24.03.06.Cara.Mell.XXX.mp4")
    assert db.studio_counts(True) == [("BrazzersExxtra", 2, "brazzersexxtra")]


def test_relocating_a_library_keeps_every_detail(tmp_path):
    db, lib = make_db(tmp_path)
    video_id = insert(db, lib, tmp_path, SCENE)
    db.set_fields(video_id, rating=5, favorite=1)
    db.set_performers(video_id, ["Cara Mell"])

    moved = db.relocate_library(lib, "\\\\server\\share\\")
    assert moved == 1
    video = db.get_video(video_id)
    assert video["path"] == os.path.join("\\\\server\\share", SCENE)
    assert video["rating"] == 5 and video["favorite"] == 1
    assert video["performers"] == ["Cara Mell"]
    assert db.libraries()[0]["path"] == "\\\\server\\share"


def test_trash_roundtrip(tmp_path):
    db, lib = make_db(tmp_path)
    vid = insert(db, lib, tmp_path, SCENE)
    db.mark_trashed([vid], "item", "origin")
    assert db.list_videos(show_hidden=True) == []
    db.restore("item")
    assert len(db.list_videos(show_hidden=True)) == 1
    db.mark_trashed([vid], "item", "origin")
    assert db.purge("item") == [vid]
    assert db.get_video(vid) is None
    assert db.known_performers() == {}


def test_a_library_from_the_old_name_is_brought_forward(tmp_path, monkeypatch):
    """The app used to be called Vault; nobody should open BlueShelf and find an empty library."""
    from blueshelf import paths

    local = tmp_path / "LocalAppData"
    old = local / "Vault"
    (old / "thumbs").mkdir(parents=True)
    (old / "library.db").write_bytes(b"pretend database")
    (old / "thumbs" / "7.jpg").write_bytes(b"jpegdata")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("BLUESHELF_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "_migrated", False)

    moved = paths.data_dir()
    assert moved == local / "BlueShelf"
    assert (moved / "library.db").read_bytes() == b"pretend database"
    assert (moved / "thumbs" / "7.jpg").exists()


def test_a_fresh_install_does_not_invent_a_library(tmp_path, monkeypatch):
    from blueshelf import paths

    local = tmp_path / "LocalAppData"
    local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("BLUESHELF_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "_migrated", False)

    fresh = paths.data_dir()
    assert fresh.is_dir() and not (fresh / "library.db").exists()


def test_each_folder_can_be_counted_and_browsed_on_its_own(tmp_path):
    """Two library folders are two places, not one merged heap."""
    from blueshelf.parser import parse_path

    db = Database(str(tmp_path / "library.db"))
    downloads = db.add_library(str(tmp_path / "downloads"))
    sorted_out = db.add_library(str(tmp_path / "sorted"))
    for library_id, name in ((downloads, "A.Scene.mp4"), (downloads, "B.Scene.mp4"), (sorted_out, "C.Scene.mp4")):
        db.insert_video(library_id, str(tmp_path / name), name, 10, 1.0, parse_path(name), True)
    db.commit()

    counts = {row["path"]: row["n"] for row in db.library_counts(False)}
    assert counts == {str(tmp_path / "downloads"): 2, str(tmp_path / "sorted"): 1}
    assert len(db.list_videos(library_id=downloads)) == 2
    assert [v["rel_path"] for v in db.list_videos(library_id=sorted_out)] == ["C.Scene.mp4"]
    assert len(db.list_videos()) == 3, "and everything together is still everything"
    db.close()


def test_plays_are_counted_by_where_they_happened(tmp_path):
    from blueshelf.parser import parse_path

    db = Database(str(tmp_path / "library.db"))
    library_id = db.add_library(str(tmp_path))
    video_id = db.insert_video(library_id, str(tmp_path / "A.mp4"), "A.mp4", 10, 1.0, parse_path("A.mp4"), True)
    other = db.insert_video(library_id, str(tmp_path / "B.mp4"), "B.mp4", 10, 1.0, parse_path("B.mp4"), True)
    db.commit()

    db.record_play(video_id)                 # the desktop player
    db.record_play(video_id, "deovr")
    db.record_play(video_id, "deovr")
    db.record_play(other, "dlna")

    assert db.play_counts(video_id) == {"desktop": 1, "deovr": 2}
    assert db.play_counts() == {"desktop": 1, "deovr": 2, "dlna": 1}
    assert db.get_video(video_id)["play_count"] == 3, "the old count still counts everything"
    assert [play["source"] for play in db.recent_plays(2)] == ["dlna", "deovr"]
    db.close()
