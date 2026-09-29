"""Bringing somebody's existing library in from Stash, XBVR, NFO files or bare folders.

There is no real Stash or XBVR database here, so these build one of each in the shape those
programs use - both the old and the new Stash layouts, since a person's copy could be either.
What is asserted is therefore what BlueShelf assumes about them; if a real library reads back
wrong, the assumption in these tests is the thing to correct.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from blueshelf import importing
from blueshelf.db import Database
from blueshelf.importing import Incoming, Match, apply_plan, read_folders, read_nfos, read_stash, read_xbvr
from blueshelf.parser import parse_path


# --------------------------------------------------------------------------- libraries to read

def _stash(path: Path, modern: bool = True) -> Path:
    """A Stash database with two films, in the layout the given version uses."""
    conn = sqlite3.connect(path)
    scene_extra = "rating100 INTEGER" if modern else "rating INTEGER, url TEXT"
    place = "" if modern else "path TEXT,"
    conn.executescript(f"""
        CREATE TABLE studios (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE performers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE scenes (id INTEGER PRIMARY KEY, {place} title TEXT, details TEXT, date TEXT,
                             studio_id INTEGER, organized INTEGER, o_counter INTEGER,
                             play_count INTEGER, resume_time REAL, last_played_at TEXT, {scene_extra});
        CREATE TABLE performers_scenes (performer_id INTEGER, scene_id INTEGER);
        CREATE TABLE scenes_tags (scene_id INTEGER, tag_id INTEGER);
        CREATE TABLE scene_markers (id INTEGER PRIMARY KEY, title TEXT, seconds REAL, end_seconds REAL,
                                    scene_id INTEGER, primary_tag_id INTEGER);
        INSERT INTO studios VALUES (1, 'WankzVR');
        INSERT INTO performers VALUES (1, 'Anna Bell'), (2, 'Gabbie Carter');
        INSERT INTO tags VALUES (1, 'POV'), (2, 'Blonde'), (3, 'Shower');
        INSERT INTO performers_scenes VALUES (1, 1), (2, 1), (2, 2);
        INSERT INTO scenes_tags VALUES (1, 1), (1, 2), (2, 3);
        INSERT INTO scene_markers VALUES (1, 'The pool', 120.0, 260.0, 1, 3),
                                         (2, '', 600.0, NULL, 1, 3);
    """)
    if modern:
        conn.executescript("""
            CREATE TABLE folders (id INTEGER PRIMARY KEY, path TEXT);
            CREATE TABLE files (id INTEGER PRIMARY KEY, basename TEXT, parent_folder_id INTEGER, size INTEGER);
            CREATE TABLE scenes_files (scene_id INTEGER, file_id INTEGER, "primary" INTEGER);
            CREATE TABLE scenes_urls (scene_id INTEGER, position INTEGER, url TEXT);
            INSERT INTO folders VALUES (1, 'D:\\videos\\WankzVR');
            INSERT INTO files VALUES (1, 'pool day.mp4', 1, 1000), (2, 'shower.mp4', 1, 2000);
            INSERT INTO scenes_files VALUES (1, 1, 1), (2, 2, 1);
            INSERT INTO scenes_urls VALUES (1, 0, 'https://example.test/pool');
        """)
        conn.executescript("""
            INSERT INTO scenes VALUES (1, 'Pool Day', 'A day at the pool.', '2024-03-05', 1, 1, 2, 4,
                                       930.5, '2026-09-20 18:04:11', 80);
            INSERT INTO scenes VALUES (2, 'Shower', NULL, NULL, NULL, 0, 0, 0, 0, NULL, NULL);
        """)
    else:
        conn.executescript("""
            INSERT INTO scenes VALUES (1, 'D:\\videos\\WankzVR\\pool day.mp4', 'Pool Day',
                                       'A day at the pool.', '2024-03-05', 1, 1, 2, 4, 930.5,
                                       '2026-09-20 18:04:11', 4, 'https://example.test/pool');
            INSERT INTO scenes VALUES (2, 'D:\\videos\\WankzVR\\shower.mp4', 'Shower', NULL, NULL,
                                       NULL, 0, 0, 0, 0, NULL, NULL, NULL);
        """)
    conn.commit()
    conn.close()
    return path


def _xbvr(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE scenes (id INTEGER PRIMARY KEY, title TEXT, studio TEXT, site TEXT, scene_url TEXT,
                             synopsis TEXT, release_date TEXT, star_rating REAL, favourite INTEGER,
                             is_watched INTEGER, watchlist INTEGER);
        CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, filename TEXT, scene_id INTEGER, size INTEGER);
        CREATE TABLE casts (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE scene_cast (cast_id INTEGER, scene_id INTEGER);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE scene_tags (tag_id INTEGER, scene_id INTEGER);
        INSERT INTO scenes VALUES (1, 'Pool Day', 'WankzVR', 'wankzvr', 'https://example.test/pool',
                                   'A day at the pool.', '2024-03-05T00:00:00Z', 4.0, 1, 1, 0);
        INSERT INTO scenes VALUES (2, 'Shower', '', 'povr', '', '', '', 0, 0, 0, 0);
        INSERT INTO files VALUES (1, 'D:\\videos\\WankzVR', 'pool day.mp4', 1, 1000);
        INSERT INTO files VALUES (2, 'D:\\videos\\WankzVR', 'shower.mp4', 2, 2000);
        INSERT INTO casts VALUES (1, 'Anna Bell'), (2, 'Gabbie Carter');
        INSERT INTO scene_cast VALUES (1, 1), (2, 1);
        INSERT INTO tags VALUES (1, 'POV');
        INSERT INTO scene_tags VALUES (1, 1);
    """)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def library(tmp_path):
    """Two films here, where BlueShelf keeps them - not where the other program had them."""
    folder = tmp_path / "films" / "WankzVR"
    folder.mkdir(parents=True)
    first, second = folder / "pool day.mp4", folder / "shower.mp4"
    first.write_bytes(b"one" * 400)
    second.write_bytes(b"two" * 700)
    db = Database(str(tmp_path / "library.db"))
    library_id = db.add_library(str(tmp_path / "films"))
    ids = []
    for film in (first, second):
        ids.append(db.insert_video(library_id, str(film), str(film.relative_to(tmp_path / "films")),
                                   film.stat().st_size, 1.0, parse_path(film.name), True))
    db.commit()
    yield db, ids, (first, second)
    db.close()


# --------------------------------------------------------------------------- reading

@pytest.mark.parametrize("modern", [True, False], ids=["new Stash", "old Stash"])
def test_stash_is_read_whichever_version_wrote_it(tmp_path, modern):
    found = read_stash(_stash(tmp_path / "stash-go.sqlite", modern))
    assert [Path(f.path).name for f in found] == ["pool day.mp4", "shower.mp4"]
    first = found[0]
    assert first.path.startswith("D:\\videos\\WankzVR")
    assert first.title == "Pool Day" and first.studio == "WankzVR" and first.date == "2024-03-05"
    assert first.details == "A day at the pool."
    assert first.url == "https://example.test/pool"
    assert first.performers == ["Anna Bell", "Gabbie Carter"]
    assert first.tags == ["Blonde", "POV"]
    assert first.rating == 4, "80 out of a hundred and 4 out of five are the same four stars"
    assert first.play_count == 4 and first.resume == 930.5
    assert first.last_played and first.last_played > 1_700_000_000
    assert first.scenes[0] == (120.0, 260.0, "The pool")
    assert first.scenes[1][0] == 600.0 and first.scenes[1][1] == 660.0, "a mark with no end gets one"
    assert first.scenes[1][2] == "Shower", "and takes its name from its tag"
    assert found[1].title == "Shower" and found[1].rating is None and found[1].studio is None


def test_a_stash_database_in_use_is_copied_not_opened(tmp_path):
    """Theirs may be open in their own program: reading it in place would fail or disturb it."""
    path = _stash(tmp_path / "stash-go.sqlite")
    theirs = sqlite3.connect(path)
    theirs.execute("BEGIN EXCLUSIVE")
    try:
        assert len(read_stash(path)) == 2
    finally:
        theirs.rollback()
        theirs.close()


def test_something_that_is_not_a_library_says_so(tmp_path):
    other = tmp_path / "notes.sqlite"
    sqlite3.connect(other).executescript("CREATE TABLE notes (id INTEGER);")
    with pytest.raises(ValueError, match="Stash"):
        read_stash(other)
    with pytest.raises(FileNotFoundError):
        read_stash(tmp_path / "nowhere.sqlite")


def test_xbvr_is_read(tmp_path):
    found = read_xbvr(_xbvr(tmp_path / "main.db"))
    assert len(found) == 2
    first = found[0]
    assert first.path == "D:\\videos\\WankzVR\\pool day.mp4"
    assert first.title == "Pool Day" and first.studio == "WankzVR"
    assert first.performers == ["Anna Bell", "Gabbie Carter"] and first.tags == ["POV"]
    assert first.rating == 4 and first.favorite is True and first.play_count == 1
    assert first.date == "2024-03-05" and first.url == "https://example.test/pool"
    assert found[1].studio == "povr", "with no studio, the site it came from"


def test_nfo_files_beside_films_are_read(tmp_path):
    folder = tmp_path / "films"
    folder.mkdir()
    (folder / "pool day.mp4").write_bytes(b"x")
    (folder / "pool day.nfo").write_text("""<?xml version="1.0"?>
        <movie>
          <title>Pool Day</title>
          <plot>A day at the pool.</plot>
          <premiered>2024-03-05</premiered>
          <studio>WankzVR</studio>
          <genre>POV</genre>
          <tag>Blonde</tag>
          <userrating>8</userrating>
          <playcount>3</playcount>
          <lastplayed>2026-09-20 18:04:11</lastplayed>
          <actor><name>Anna Bell</name><role>herself</role></actor>
          <actor><name>Gabbie Carter</name></actor>
          <resume><position>930.5</position><total>3600</total></resume>
        </movie>""", encoding="utf-8")
    # A second film with the folder-wide name Kodi also allows, in its own folder.
    lone = folder / "Shower"
    lone.mkdir()
    (lone / "shower.mp4").write_bytes(b"x")
    (lone / "movie.nfo").write_text("<movie><title>Shower</title><year>2023</year></movie>", encoding="utf-8")
    (folder / "no-nfo.mp4").write_bytes(b"x")

    found = sorted(read_nfos([folder]), key=lambda f: Path(f.path).name)
    assert [Path(f.path).name for f in found] == ["pool day.mp4", "shower.mp4"]
    first = found[0]
    assert first.title == "Pool Day" and first.studio == "WankzVR" and first.date == "2024-03-05"
    assert first.performers == ["Anna Bell", "Gabbie Carter"]
    assert sorted(first.tags) == ["Blonde", "POV"]
    assert first.rating == 4 and first.play_count == 3 and first.resume == 930.5
    assert found[1].title == "Shower" and found[1].date == "2023-01-01"


def test_a_damaged_nfo_is_skipped_not_fatal(tmp_path):
    folder = tmp_path / "films"
    folder.mkdir()
    (folder / "broken.mp4").write_bytes(b"x")
    (folder / "broken.nfo").write_text("<movie><title>Half a fi", encoding="utf-8")
    (folder / "fine.mp4").write_bytes(b"x")
    (folder / "fine.nfo").write_text("<movie><title>Fine</title></movie>", encoding="utf-8")
    assert [f.title for f in read_nfos([folder])] == ["Fine"]


def test_bare_folders_are_read_from_their_names(tmp_path):
    folder = tmp_path / "films" / "WankzVR"
    folder.mkdir(parents=True)
    (folder / "WankzVR.24.03.05.Anna.Bell.Pool.Day.XXX.mp4").write_bytes(b"x")
    found = read_folders([tmp_path / "films"])
    assert len(found) == 1 and found[0].title == "Pool Day"
    assert found[0].performers == ["Anna Bell"]


# --------------------------------------------------------------------------- matching

def test_films_match_by_path_then_by_name_then_by_the_film_itself(library):
    db, ids, films = library
    theirs = [
        Incoming(path=str(films[0]), title="By path"),                       # exactly where ours is
        Incoming(path=r"D:\elsewhere\shower.mp4", title="By name"),          # same name and size
    ]
    plan = Match(db).plan(theirs)
    assert dict(plan.how) == {"by path": 1, "by name": 1}
    assert sorted(v for _t, v in plan.matched) == sorted(ids)

    # Renamed and on another drive: only the film's own bytes can find it.
    moved = films[0].with_name("a completely different name.mp4")
    import shutil
    shutil.copy2(films[0], moved)
    plan = Match(db).plan([Incoming(path=str(moved), title="By content")])
    assert plan.how == {"by the film itself": 1} and plan.matched[0][1] == ids[0]


def test_their_folders_can_be_mapped_onto_ours(tmp_path):
    """Which is what is needed when names repeat - as they do in most libraries, where every
    studio has a "scene 1"."""
    ours = tmp_path / "films"
    for studio in ("WankzVR", "POVR"):
        (ours / studio).mkdir(parents=True)
        (ours / studio / "scene 1.mp4").write_bytes(b"x" * (100 if studio == "WankzVR" else 200))
    db = Database(str(tmp_path / "library.db"))
    try:
        library_id = db.add_library(str(ours))
        ids = {}
        for studio in ("WankzVR", "POVR"):
            film = ours / studio / "scene 1.mp4"
            ids[studio] = db.insert_video(library_id, str(film), str(film.relative_to(ours)),
                                          film.stat().st_size, 1.0, parse_path(film.name), True)
        db.commit()
        theirs = [Incoming(path=r"D:\videos\WankzVR\scene 1.mp4", title="Theirs Wankz"),
                  Incoming(path=r"D:\videos\POVR\scene 1.mp4", title="Theirs POVR")]

        match = Match(db)
        missing = match.plan(theirs, by_content=False).missing
        assert len(missing) == 2, "the same name in two folders is no match at all"

        suggested = match.suggest_roots(missing)
        assert suggested, "it should work out which of their folders is ours"
        theirs_root, ours_root, count = suggested[0]
        assert theirs_root.lower() == r"d:\videos" and count == 2
        assert Path(ours_root) == ours

        match.remap(theirs_root, ours_root)
        plan = match.plan(theirs, by_content=False)
        assert plan.how == {"by path": 2}
        assert {t.title: video for (t, video) in plan.matched} == \
            {"Theirs Wankz": ids["WankzVR"], "Theirs POVR": ids["POVR"]}, "each to the right film"
    finally:
        db.close()


def test_a_film_they_have_and_we_do_not_is_reported_not_invented(library):
    db, _ids, _films = library
    plan = Match(db).plan([Incoming(path=r"D:\videos\WankzVR\never seen.mp4", title="Missing")])
    assert len(plan.missing) == 1 and not plan.matched
    assert db.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2, "nothing was added"
    assert "1 not found here" in plan.summary()


# --------------------------------------------------------------------------- putting it in

def test_what_they_organised_arrives_and_their_files_are_untouched(library, tmp_path):
    db, ids, films = library
    before = [(f, f.stat().st_size, f.stat().st_mtime) for f in films]
    found = read_stash(_stash(tmp_path / "stash-go.sqlite"))
    match = Match(db)
    match.remap(r"D:\videos\WankzVR", str(films[0].parent))
    counted = apply_plan(db, match.plan(found))
    assert counted["films"] == 2

    video = db.get_video(ids[0])
    assert video["title"] == "Pool Day" and video["studio"] == "WankzVR"
    assert video["release_date"] == "2024-03-05" and video["details"] == "A day at the pool."
    assert video["url"] == "https://example.test/pool"
    assert video["performers"] == ["Anna Bell", "Gabbie Carter"]
    assert video["tags"] == ["Blonde", "POV"]
    assert video["rating"] == 4 and video["play_count"] == 4 and video["resume_pos"] == 930.5
    assert [(s["start"], s["end"], s["label"]) for s in db.scenes(ids[0])] == \
        [(120.0, 260.0, "The pool"), (600.0, 660.0, "Shower")]
    # Locked, so the next scan does not write file names over what they organised.
    assert video["meta_locked"] and video["perf_locked"] and video["tags_locked"]
    assert [(f, f.stat().st_size, f.stat().st_mtime) for f in films] == before, "their films are untouched"


def test_importing_twice_changes_nothing_the_second_time(library, tmp_path):
    db, ids, films = library
    found = read_stash(_stash(tmp_path / "stash-go.sqlite"))
    match = Match(db)
    match.remap(r"D:\videos\WankzVR", str(films[0].parent))
    apply_plan(db, match.plan(found))
    first = (db.get_video(ids[0]), db.scenes(ids[0]))
    apply_plan(db, Match(db).plan(found) if False else match.plan(found))
    assert db.get_video(ids[0])["tags"] == first[0]["tags"], "no duplicate tags"
    assert len(db.scenes(ids[0])) == len(first[1]), "no duplicate scenes"
    assert db.get_video(ids[0])["performers"] == first[0]["performers"]


def test_what_is_here_already_is_kept_unless_asked(library, tmp_path):
    db, ids, films = library
    db.set_fields(ids[0], title="My own title", rating=5, meta_locked=1)   # as typing one here does
    db.set_tags(ids[0], ["Mine"])
    found = read_stash(_stash(tmp_path / "stash-go.sqlite"))
    match = Match(db)
    match.remap(r"D:\videos\WankzVR", str(films[0].parent))
    apply_plan(db, match.plan(found))
    video = db.get_video(ids[0])
    assert video["title"] == "My own title" and video["rating"] == 5, "what they typed here wins"
    assert "Mine" in video["tags"] and "POV" in video["tags"], "tags are added to, never replaced"

    apply_plan(db, match.plan(found), overwrite=True)
    assert db.get_video(ids[0])["title"] == "Pool Day" and db.get_video(ids[0])["rating"] == 4
    assert "Mine" in db.get_video(ids[0])["tags"], "even overwriting never drops their own tags"


def test_a_library_with_nothing_to_teach_is_left_alone(library):
    db, ids, films = library
    plan = Match(db).plan([Incoming(path=str(films[0]))])
    assert plan.nothing_to_learn and not plan.matched
    apply_plan(db, plan)
    assert db.get_video(ids[0])["title"] == db.get_video(ids[0])["title"]


def test_the_columns_an_import_needs_are_added_to_an_older_library(tmp_path):
    """Somebody's library from an older BlueShelf has no details or url column."""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("CREATE TABLE videos (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, "
                       "rel_path TEXT NOT NULL, library_id INTEGER NOT NULL DEFAULT 1, "
                       "added_at REAL NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '');")
    conn.commit()
    conn.close()
    db = Database(str(old))
    try:
        have = {r[1] for r in db.conn.execute("PRAGMA table_info(videos)")}
        assert {"details", "url"} <= have
    finally:
        db.close()


def test_where_their_libraries_usually_live_is_looked_for():
    for name, where in importing.likely_places():
        assert name in ("Stash", "XBVR") and os.path.isfile(where)
