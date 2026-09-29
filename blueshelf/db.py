"""SQLite storage for the library.

A Database wraps one connection; background threads must create their own.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

from blueshelf.parser import Parsed, performer_key, smart_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    path TEXT UNIQUE NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    added_at REAL NOT NULL,
    duration REAL,
    width INTEGER,
    height INTEGER,
    vcodec TEXT,
    bitrate INTEGER,
    fps REAL,
    title TEXT NOT NULL DEFAULT '',
    studio TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    is_vr INTEGER NOT NULL DEFAULT 0,
    vr_layout INTEGER NOT NULL DEFAULT 1,
    vr_proj INTEGER NOT NULL DEFAULT 0,
    vr_lens_fov REAL NOT NULL DEFAULT 190,
    vr_locked INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    hidden_locked INTEGER NOT NULL DEFAULT 0,
    favorite INTEGER NOT NULL DEFAULT 0,
    rating INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    last_played REAL,
    resume_pos REAL NOT NULL DEFAULT 0,
    meta_locked INTEGER NOT NULL DEFAULT 0,
    perf_locked INTEGER NOT NULL DEFAULT 0,
    tags_locked INTEGER NOT NULL DEFAULT 0,
    probed INTEGER NOT NULL DEFAULT 0,
    thumb TEXT,
    quick_hash TEXT,
    phash TEXT,
    trashed INTEGER NOT NULL DEFAULT 0,
    trash_item TEXT,
    trash_origin TEXT,
    trashed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_videos_library ON videos(library_id);
CREATE TABLE IF NOT EXISTS performers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key TEXT UNIQUE NOT NULL,
    favorite INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS video_performers (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    performer_id INTEGER NOT NULL REFERENCES performers(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, performer_id)
);
CREATE INDEX IF NOT EXISTS idx_vp_performer ON video_performers(performer_id);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_vt_tag ON video_tags(tag_id);
CREATE TABLE IF NOT EXISTS plays (
    id         INTEGER PRIMARY KEY,
    video_id   INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    at         REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'desktop'
);
CREATE INDEX IF NOT EXISTS plays_by_video ON plays(video_id);
CREATE INDEX IF NOT EXISTS plays_by_source ON plays(source, at);

CREATE TABLE IF NOT EXISTS scans (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    fps REAL NOT NULL DEFAULT 0,
    frames INTEGER NOT NULL DEFAULT 0,
    reading TEXT NOT NULL DEFAULT '',
    stretches TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Stretches of a film marked while watching, to be made into passthrough copies of their own.
CREATE TABLE IF NOT EXISTS scenes (
    id       INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    start_at REAL NOT NULL,
    end_at   REAL NOT NULL,
    label    TEXT NOT NULL DEFAULT '',
    made_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS scenes_by_video ON scenes(video_id, start_at);
"""

SORTS = {
    "added": "v.added_at",
    "title": "v.title COLLATE NOCASE",
    "date": "v.release_date",
    "duration": "v.duration",
    "size": "v.size",
    "resolution": "(COALESCE(v.width, 0) * COALESCE(v.height, 0))",
    "rating": "v.rating",
    "plays": "v.play_count",
    "last_played": "v.last_played",
    "studio": "v.studio COLLATE NOCASE",
    "performers": "performer_names COLLATE NOCASE",
}

VIEWS = {
    "all": None,
    "recent": "v.added_at >= strftime('%s', 'now') - 30 * 86400",
    "continue": "v.resume_pos > 0",
    "favorites": "v.favorite = 1",
    "unwatched": "v.play_count = 0",
    "vr": "v.is_vr = 1",
    "flat": "v.is_vr = 0",
    "no_performers": "NOT EXISTS (SELECT 1 FROM video_performers x WHERE x.video_id = v.id)",
    "hidden": "v.hidden = 1",
}

EDITABLE_FIELDS = {"title", "studio", "release_date", "is_vr", "vr_layout", "vr_proj", "vr_lens_fov",
                   "vr_locked", "hidden", "hidden_locked", "favorite", "rating", "meta_locked", "resume_pos",
                   "duration", "width", "height", "vcodec", "bitrate", "fps", "probed", "thumb",
                   "quick_hash", "phash", "details", "url",
                   # Set when a library is brought in from another program.
                   "play_count", "last_played", "perf_locked", "tags_locked"}


def studio_key(name: str) -> str:
    return "".join(name.lower().split())


def _better_display(old: str, new: str) -> str:
    """Prefer the variant with more deliberate capitalisation."""
    def score(s: str) -> int:
        return 0 if s.isupper() else sum(c.isupper() for c in s)
    return new if score(new) > score(old) else old


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._add_late_columns()
        self.conn.commit()

    # Columns added after people already had libraries. CREATE TABLE IF NOT EXISTS does nothing
    # to a table that is already there, so they are added here or an older library would be
    # missing them for good.
    LATE_COLUMNS = {"videos": {"details": "TEXT NOT NULL DEFAULT ''", "url": "TEXT NOT NULL DEFAULT ''"}}

    def _add_late_columns(self) -> None:
        for table, columns in self.LATE_COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------ settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
        self.conn.commit()

    # ------------------------------------------------------------ libraries

    def add_library(self, path: str) -> int:
        self.conn.execute("INSERT OR IGNORE INTO libraries(path) VALUES (?)", (path,))
        self.conn.commit()
        return self.conn.execute("SELECT id FROM libraries WHERE path = ?", (path,)).fetchone()["id"]

    def relocate_library(self, library_id: int, new_root: str) -> int:
        """Point a library at a different folder, keeping every video's details.

        For when a drive letter changes or a share moves. Only the folder in front of each video
        changes, so ratings, performers and categories survive - removing and re-adding loses them.
        """
        new_root = new_root.rstrip("\\/")
        moved = 0
        for row in self.conn.execute("SELECT id, rel_path FROM videos WHERE library_id = ?",
                                     (library_id,)).fetchall():
            try:
                self.conn.execute("UPDATE videos SET path = ? WHERE id = ?",
                                  (os.path.join(new_root, row["rel_path"]), row["id"]))
                moved += 1
            except sqlite3.IntegrityError:
                pass  # something else already sits at that path
        self.conn.execute("UPDATE libraries SET path = ? WHERE id = ?", (new_root, library_id))
        self.conn.commit()
        return moved

    def remove_library(self, library_id: int) -> None:
        self.conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
        self.cleanup_orphans()

    def libraries(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM libraries ORDER BY path").fetchall()

    # ------------------------------------------------------------ scanning

    def existing_files(self, library_id: int) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT id, path, size, mtime FROM videos WHERE library_id = ? AND trashed = 0",
                                 (library_id,)).fetchall()
        return {r["path"]: r for r in rows}

    def insert_video(self, library_id: int, path: str, rel_path: str, size: int, mtime: float,
                     parsed: Parsed, hide_non_adult: bool) -> int:
        cur = self.conn.execute(
            "INSERT INTO videos(library_id, path, rel_path, size, mtime, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (library_id, path, rel_path, size, mtime, min(mtime, time.time())))
        video_id = cur.lastrowid
        self.apply_parsed(video_id, parsed, hide_non_adult)
        return video_id

    def relocate_video(self, video_id: int, path: str, rel_path: str) -> None:
        """The same file, in a new place. Everything known about it stays attached to it."""
        self.conn.execute("UPDATE videos SET path = ?, rel_path = ? WHERE id = ?", (path, rel_path, video_id))
        self.conn.commit()

    def file_changed(self, video_id: int, rel_path: str, size: int, mtime: float) -> None:
        self.conn.execute("UPDATE videos SET rel_path = ?, size = ?, mtime = ?, probed = 0, thumb = NULL, "
                          "quick_hash = NULL, phash = NULL WHERE id = ?", (rel_path, size, mtime, video_id))

    def apply_parsed(self, video_id: int, parsed: Parsed, hide_non_adult: bool, force: bool = False) -> None:
        row = self.conn.execute("SELECT meta_locked, perf_locked, tags_locked, vr_locked, hidden_locked "
                                "FROM videos WHERE id = ?", (video_id,)).fetchone()
        if force or not row["meta_locked"]:
            self.conn.execute("UPDATE videos SET title = ?, studio = ?, release_date = ? WHERE id = ?",
                              (parsed.title, parsed.studio, parsed.date, video_id))
        if force or not row["vr_locked"]:
            vr = parsed.vr
            self.conn.execute("UPDATE videos SET is_vr = ?, vr_layout = ?, vr_proj = ?, vr_lens_fov = ? "
                              "WHERE id = ?", (int(vr.is_vr), vr.layout, vr.proj, vr.lens_fov, video_id))
        if force or not row["hidden_locked"]:
            self.conn.execute("UPDATE videos SET hidden = ? WHERE id = ?",
                              (int(hide_non_adult and not parsed.adult), video_id))
        if force or not row["perf_locked"]:
            self.conn.execute("DELETE FROM video_performers WHERE video_id = ?", (video_id,))
            for name in parsed.performers:
                self.link_performer(video_id, self.performer_id(name))
        if force or not row["tags_locked"]:
            self.conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
            for name in parsed.tags:
                self.link_tag(video_id, self.tag_id(name))
        if force:
            self.conn.execute("UPDATE videos SET meta_locked = 0, perf_locked = 0, tags_locked = 0, "
                              "vr_locked = 0, hidden_locked = 0 WHERE id = ?", (video_id,))

    # ------------------------------------------------------------ scenes

    SHORTEST_SCENE = 2.0   # seconds; anything shorter is a slip of the finger

    def scenes(self, video_id: int) -> list[dict]:
        """A film's marked scenes in the order they happen, numbered as the viewer sees them."""
        rows = self.conn.execute("SELECT id, start_at, end_at, label FROM scenes WHERE video_id = ? "
                                 "ORDER BY start_at, id", (video_id,)).fetchall()
        return [{"id": r["id"], "start": r["start_at"], "end": r["end_at"],
                 "label": r["label"] or f"Scene {n}", "named": bool(r["label"])}
                for n, r in enumerate(rows, 1)]

    def add_scene(self, video_id: int, start: float, end: float, label: str = "") -> int:
        start, end = sorted((max(0.0, float(start)), max(0.0, float(end))))
        if end - start < self.SHORTEST_SCENE:
            raise ValueError(f"a scene needs to be at least {self.SHORTEST_SCENE:g} seconds long")
        cursor = self.conn.execute("INSERT INTO scenes(video_id, start_at, end_at, label, made_at) "
                                   "VALUES (?, ?, ?, ?, strftime('%s', 'now'))",
                                   (video_id, start, end, (label or "").strip()[:80]))
        self.conn.commit()
        return cursor.lastrowid

    def update_scene(self, scene_id: int, start: float | None = None, end: float | None = None,
                     label: str | None = None) -> None:
        row = self.conn.execute("SELECT start_at, end_at FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        if row is None:
            raise ValueError("no such scene")
        new_start = row["start_at"] if start is None else max(0.0, float(start))
        new_end = row["end_at"] if end is None else max(0.0, float(end))
        new_start, new_end = sorted((new_start, new_end))
        if new_end - new_start < self.SHORTEST_SCENE:
            raise ValueError(f"a scene needs to be at least {self.SHORTEST_SCENE:g} seconds long")
        self.conn.execute("UPDATE scenes SET start_at = ?, end_at = ? WHERE id = ?", (new_start, new_end, scene_id))
        if label is not None:
            self.conn.execute("UPDATE scenes SET label = ? WHERE id = ?", (label.strip()[:80], scene_id))
        self.conn.commit()

    def delete_scene(self, scene_id: int) -> None:
        self.conn.execute("DELETE FROM scenes WHERE id = ?", (scene_id,))
        self.conn.commit()

    def scene_video(self, scene_id: int) -> int | None:
        row = self.conn.execute("SELECT video_id FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        return row["video_id"] if row else None

    def videos_with_scenes(self) -> list[dict]:
        """Films with scenes marked, most recently marked first, for choosing what to make."""
        rows = self.conn.execute(
            "SELECT v.id, v.path, v.title, COUNT(s.id) AS n, MAX(s.made_at) AS latest "
            "FROM scenes s JOIN videos v ON v.id = s.video_id WHERE v.trashed = 0 "
            "GROUP BY v.id ORDER BY latest DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_videos(self, ids: Iterable[int]) -> None:
        self.conn.executemany("DELETE FROM videos WHERE id = ?", [(i,) for i in ids])
        self.conn.commit()

    def set_fields(self, video_id: int, **fields: Any) -> None:
        bad = set(fields) - EDITABLE_FIELDS
        if bad:
            raise ValueError(f"not editable: {bad}")
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE videos SET {assignments} WHERE id = ?", (*fields.values(), video_id))
        self.conn.commit()

    def unprobed(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, path, rel_path, is_vr, vr_locked FROM videos "
                                 "WHERE probed = 0 AND trashed = 0").fetchall()

    def missing_thumbs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, path, duration, is_vr, vr_layout, vr_proj, vr_lens_fov FROM videos "
                                 "WHERE thumb IS NULL AND probed = 1 AND trashed = 0 "
                                 "ORDER BY added_at DESC").fetchall()

    def folder_video_count(self, library_id: int, folder: str) -> int:
        rows = self.conn.execute("SELECT rel_path FROM videos WHERE library_id = ? AND trashed = 0", (library_id,))
        return sum(1 for r in rows if os.path.dirname(r["rel_path"]) == folder)

    # ------------------------------------------------------------ performers and tags

    def performer_id(self, name: str) -> int:
        name = smart_title(name.strip())
        key = performer_key(name)
        row = self.conn.execute("SELECT id, name FROM performers WHERE key = ?", (key,)).fetchone()
        if row:
            better = _better_display(row["name"], name)
            if better != row["name"]:
                self.conn.execute("UPDATE performers SET name = ? WHERE id = ?", (better, row["id"]))
            return row["id"]
        return self.conn.execute("INSERT INTO performers(name, key) VALUES (?, ?)", (name, key)).lastrowid

    def tag_id(self, name: str) -> int:
        name = name.strip()
        key = name.lower()
        row = self.conn.execute("SELECT id FROM tags WHERE key = ?", (key,)).fetchone()
        if row:
            return row["id"]
        return self.conn.execute("INSERT INTO tags(name, key) VALUES (?, ?)", (name, key)).lastrowid

    def link_performer(self, video_id: int, performer_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO video_performers(video_id, performer_id) VALUES (?, ?)",
                          (video_id, performer_id))

    def link_tag(self, video_id: int, tag_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)", (video_id, tag_id))

    def set_performers(self, video_id: int, names: list[str]) -> None:
        self.conn.execute("DELETE FROM video_performers WHERE video_id = ?", (video_id,))
        for name in names:
            if name.strip():
                self.link_performer(video_id, self.performer_id(name))
        self.conn.execute("UPDATE videos SET perf_locked = 1 WHERE id = ?", (video_id,))
        self.cleanup_orphans()

    def set_tags(self, video_id: int, names: list[str]) -> None:
        self.conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
        for name in names:
            if name.strip():
                self.link_tag(video_id, self.tag_id(name))
        self.conn.execute("UPDATE videos SET tags_locked = 1 WHERE id = ?", (video_id,))
        self.cleanup_orphans()

    def add_performer_to(self, video_ids: Iterable[int], name: str) -> None:
        pid = self.performer_id(name)
        for vid in video_ids:
            self.link_performer(vid, pid)
            self.conn.execute("UPDATE videos SET perf_locked = 1 WHERE id = ?", (vid,))
        self.conn.commit()

    def add_tag_to(self, video_ids: Iterable[int], name: str) -> None:
        tid = self.tag_id(name)
        for vid in video_ids:
            self.link_tag(vid, tid)
            self.conn.execute("UPDATE videos SET tags_locked = 1 WHERE id = ?", (vid,))
        self.conn.commit()

    def video_performer_names(self, video_id: int) -> list[str]:
        return [r["name"] for r in self.conn.execute(
            "SELECT p.name FROM video_performers x JOIN performers p ON p.id = x.performer_id "
            "WHERE x.video_id = ? ORDER BY p.name", (video_id,))]

    def performer_video_titles(self) -> dict[int, list[str]]:
        rows = self.conn.execute(
            "SELECT x.performer_id AS pid, v.title AS title FROM video_performers x "
            "JOIN videos v ON v.id = x.video_id AND v.trashed = 0")
        titles: dict[int, list[str]] = {}
        for r in rows:
            titles.setdefault(r["pid"], []).append(r["title"])
        return titles

    def rename_performer(self, performer_id: int, new_name: str) -> None:
        """Rename, merging into an existing performer when the new name already exists."""
        new_name = new_name.strip()
        key = performer_key(new_name)
        if not key:
            return
        other = self.conn.execute("SELECT id FROM performers WHERE key = ? AND id != ?",
                                  (key, performer_id)).fetchone()
        if other:
            self.conn.execute("INSERT OR IGNORE INTO video_performers(video_id, performer_id) "
                              "SELECT video_id, ? FROM video_performers WHERE performer_id = ?",
                              (other["id"], performer_id))
            self.conn.execute("DELETE FROM performers WHERE id = ?", (performer_id,))
            self.conn.execute("UPDATE performers SET name = ? WHERE id = ?", (new_name, other["id"]))
        else:
            self.conn.execute("UPDATE performers SET name = ?, key = ? WHERE id = ?", (new_name, key, performer_id))
        self.conn.commit()

    def set_performer_favorite(self, performer_id: int, favorite: bool) -> None:
        self.conn.execute("UPDATE performers SET favorite = ? WHERE id = ?", (int(favorite), performer_id))
        self.conn.commit()

    def delete_performer(self, performer_id: int) -> None:
        self.conn.execute("UPDATE videos SET perf_locked = 1 WHERE id IN "
                          "(SELECT video_id FROM video_performers WHERE performer_id = ?)", (performer_id,))
        self.conn.execute("DELETE FROM performers WHERE id = ?", (performer_id,))
        self.conn.commit()

    def rename_tag(self, tag_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            return
        other = self.conn.execute("SELECT id FROM tags WHERE key = ? AND id != ?",
                                  (new_name.lower(), tag_id)).fetchone()
        if other:
            self.conn.execute("INSERT OR IGNORE INTO video_tags(video_id, tag_id) "
                              "SELECT video_id, ? FROM video_tags WHERE tag_id = ?", (other["id"], tag_id))
            self.conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        else:
            self.conn.execute("UPDATE tags SET name = ?, key = ? WHERE id = ?", (new_name, new_name.lower(), tag_id))
        self.conn.commit()

    def delete_tag(self, tag_id: int) -> None:
        self.conn.execute("UPDATE videos SET tags_locked = 1 WHERE id IN "
                          "(SELECT video_id FROM video_tags WHERE tag_id = ?)", (tag_id,))
        self.conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        self.conn.commit()

    def rename_studio(self, old_key: str, new_name: str) -> None:
        for r in self.conn.execute("SELECT id, studio FROM videos").fetchall():
            if studio_key(r["studio"]) == old_key:
                self.conn.execute("UPDATE videos SET studio = ?, meta_locked = 1 WHERE id = ?", (new_name, r["id"]))
        self.conn.commit()

    def cleanup_orphans(self) -> None:
        self.conn.execute("DELETE FROM performers WHERE id NOT IN (SELECT performer_id FROM video_performers)")
        self.conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM video_tags)")
        self.conn.commit()

    def known_performers(self) -> dict[str, tuple[int, str]]:
        rows = self.conn.execute("SELECT id, name, key FROM performers").fetchall()
        return {r["key"]: (r["id"], r["name"]) for r in rows if " " in r["key"]}

    def linkable_videos(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, rel_path FROM videos WHERE perf_locked = 0 AND trashed = 0").fetchall()

    def performer_names(self) -> list[str]:
        return [r["name"] for r in self.conn.execute("SELECT name FROM performers ORDER BY name COLLATE NOCASE")]

    def tag_names(self) -> list[str]:
        return [r["name"] for r in self.conn.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE")]

    def studio_names(self) -> list[str]:
        return [entry[0] for entry in self.studio_counts(show_hidden=True)]

    # ------------------------------------------------------------ browsing

    def list_videos(self, *, search: str = "", view: str = "all", performer_id: int | None = None,
                    studio: str | None = None, tag_id: int | None = None, show_hidden: bool = False,
                    sort: str = "added", descending: bool = True, video_id: int | None = None,
                    library_id: int | None = None) -> list[dict]:
        where, params = ["v.trashed = 0"], []
        if video_id is not None:
            where.append("v.id = ?")
            params.append(video_id)
        if view == "hidden":
            where.append("v.hidden = 1")
        elif not show_hidden:
            where.append("v.hidden = 0")
        if VIEWS.get(view) and view != "hidden":
            where.append(VIEWS[view])
        if performer_id is not None:
            where.append("EXISTS (SELECT 1 FROM video_performers x WHERE x.video_id = v.id AND x.performer_id = ?)")
            params.append(performer_id)
        if tag_id is not None:
            where.append("EXISTS (SELECT 1 FROM video_tags y WHERE y.video_id = v.id AND y.tag_id = ?)")
            params.append(tag_id)
        if library_id is not None:
            where.append("v.library_id = ?")
            params.append(library_id)
        if studio is not None:
            where.append("lower(replace(v.studio, ' ', '')) = ?")
            params.append(studio)
        for word in search.split():
            like = f"%{word}%"
            where.append(
                "(v.title LIKE ? OR v.studio LIKE ? OR v.rel_path LIKE ? "
                "OR EXISTS (SELECT 1 FROM video_performers x JOIN performers p ON p.id = x.performer_id "
                "WHERE x.video_id = v.id AND p.name LIKE ?) "
                "OR EXISTS (SELECT 1 FROM video_tags y JOIN tags t ON t.id = y.tag_id "
                "WHERE y.video_id = v.id AND t.name LIKE ?))")
            params += [like] * 5
        order = SORTS.get(sort, SORTS["added"])
        direction = "DESC" if descending else "ASC"
        if view == "continue":
            order, direction = "v.last_played", "DESC"
        sql = f"""
            SELECT v.*,
              (SELECT group_concat(name, ', ') FROM (SELECT p.name FROM video_performers x
                 JOIN performers p ON p.id = x.performer_id WHERE x.video_id = v.id ORDER BY p.name)) AS performer_names,
              (SELECT group_concat(name, ', ') FROM (SELECT t.name FROM video_tags y
                 JOIN tags t ON t.id = y.tag_id WHERE y.video_id = v.id ORDER BY t.name)) AS tag_names
            FROM videos v
            WHERE {' AND '.join(where)}
            ORDER BY {order} {direction}, v.title COLLATE NOCASE"""
        return [dict(r) for r in self.conn.execute(sql, params)]

    # ------------------------------------------------------------ passthrough scans

    def save_scan(self, path: str, size: int, mtime: float, reading: dict,
                  stretches: list[tuple[int, int]]) -> None:
        """Keep what a scan found, and what was made of it by hand.

        Reading a video through takes a minute or two, and the corrections dragged onto the
        strip afterwards took judgement that nothing else can reproduce. Neither should be
        lost by closing a window.
        """
        self.conn.execute(
            "INSERT INTO scans(path, size, mtime, fps, frames, reading, stretches, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
            "size = excluded.size, mtime = excluded.mtime, fps = excluded.fps, "
            "frames = excluded.frames, reading = excluded.reading, "
            "stretches = excluded.stretches, at = excluded.at",
            (path, int(size), float(mtime), float(reading.get("fps") or 0),
             int(reading.get("frames") or 0), json.dumps(reading),
             json.dumps([[int(a), int(b)] for a, b in stretches]), time.time()))
        self.conn.commit()

    def get_scan(self, path: str, size: int, mtime: float) -> dict | None:
        """What was found last time, unless the file has changed since.

        A re-encode or a re-download makes the old reading meaningless, and matting a video
        against somebody else's timings would be worse than not remembering at all.
        """
        row = self.conn.execute(
            "SELECT * FROM scans WHERE path = ? COLLATE NOCASE", (path,)).fetchone()
        if row is None:
            return None
        if int(row["size"]) != int(size) or abs(float(row["mtime"]) - float(mtime)) > 2:
            return None
        try:
            reading = json.loads(row["reading"])
            stretches = [(int(a), int(b)) for a, b in json.loads(row["stretches"] or "[]")]
        except (ValueError, TypeError):
            return None
        return {"reading": reading, "stretches": stretches, "at": row["at"]}

    def forget_scan(self, path: str) -> None:
        self.conn.execute("DELETE FROM scans WHERE path = ? COLLATE NOCASE", (path,))
        self.conn.commit()

    def video_id_for_path(self, path: str) -> int | None:
        """Find a video by where it sits on disk, however the two spellings differ in case."""
        row = self.conn.execute(
            "SELECT id FROM videos WHERE path = ? COLLATE NOCASE", (path,)).fetchone()
        return row["id"] if row else None

    def get_video(self, video_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if row is None:
            return None
        video = dict(row)
        video["performers"] = self.video_performer_names(video_id)
        video["tags"] = [r["name"] for r in self.conn.execute(
            "SELECT t.name FROM video_tags y JOIN tags t ON t.id = y.tag_id WHERE y.video_id = ? ORDER BY t.name",
            (video_id,))]
        return video

    def performer_counts(self, show_hidden: bool) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.id, p.name, p.favorite, count(v.id) AS n FROM performers p "
            "JOIN video_performers x ON x.performer_id = p.id "
            "JOIN videos v ON v.id = x.video_id AND v.trashed = 0 AND (v.hidden = 0 OR ?) "
            "GROUP BY p.id ORDER BY p.favorite DESC, p.name COLLATE NOCASE", (int(show_hidden),)).fetchall()

    def library_counts(self, show_hidden: bool) -> list[sqlite3.Row]:
        """Each library folder and how many videos are in it, so folders can be browsed apart."""
        return self.conn.execute(
            "SELECT l.id, l.path, count(v.id) AS n FROM libraries l "
            "LEFT JOIN videos v ON v.library_id = l.id AND v.trashed = 0 AND (v.hidden = 0 OR ?) "
            "GROUP BY l.id ORDER BY l.path", (int(show_hidden),)).fetchall()

    def tag_counts(self, show_hidden: bool) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.id, t.name, count(v.id) AS n FROM tags t JOIN video_tags y ON y.tag_id = t.id "
            "JOIN videos v ON v.id = y.video_id AND v.trashed = 0 AND (v.hidden = 0 OR ?) "
            "GROUP BY t.id ORDER BY t.name COLLATE NOCASE", (int(show_hidden),)).fetchall()

    def studio_counts(self, show_hidden: bool) -> list[tuple[str, int, str]]:
        """(display name, count, key) per studio, merging variants that differ only by case or spaces."""
        rows = self.conn.execute(
            "SELECT studio, count(*) AS n FROM videos WHERE studio != '' AND trashed = 0 AND (hidden = 0 OR ?) "
            "GROUP BY studio", (int(show_hidden),)).fetchall()
        merged: dict[str, list] = {}
        for r in rows:
            key = studio_key(r["studio"])
            entry = merged.setdefault(key, [r["studio"], 0, key])
            entry[0] = _better_display(entry[0], r["studio"])
            entry[1] += r["n"]
        return sorted((tuple(e) for e in merged.values()), key=lambda e: e[0].lower())

    def view_counts(self, show_hidden: bool) -> dict[str, int]:
        counts = {}
        for view, clause in VIEWS.items():
            where = ["v.trashed = 0"]
            if view == "hidden":
                where.append("v.hidden = 1")
            else:
                if not show_hidden:
                    where.append("v.hidden = 0")
                if clause:
                    where.append(clause)
            counts[view] = self.conn.execute(
                f"SELECT count(*) FROM videos v WHERE {' AND '.join(where)}").fetchone()[0]
        return counts

    def totals(self, show_hidden: bool) -> tuple[int, int, float]:
        row = self.conn.execute("SELECT count(*), COALESCE(sum(size), 0), COALESCE(sum(duration), 0) FROM videos "
                                "WHERE trashed = 0 AND (hidden = 0 OR ?)", (int(show_hidden),)).fetchone()
        return row[0], row[1], row[2]

    # ------------------------------------------------------------ playback

    def record_play(self, video_id: int, source: str = "desktop") -> None:
        """One play, and where it happened: the desktop, the headset page, DeoVR or a DLNA player."""
        now = time.time()
        self.conn.execute("UPDATE videos SET play_count = play_count + 1, last_played = ? WHERE id = ?",
                          (now, video_id))
        self.conn.execute("INSERT INTO plays(video_id, at, source) VALUES (?, ?, ?)", (video_id, now, source))
        self.conn.commit()

    def play_counts(self, video_id: int | None = None) -> dict[str, int]:
        """How many plays came from each place, for one video or for the whole library."""
        sql = "SELECT source, count(*) AS n FROM plays"
        values: tuple = ()
        if video_id is not None:
            sql += " WHERE video_id = ?"
            values = (video_id,)
        return {row["source"]: row["n"] for row in self.conn.execute(sql + " GROUP BY source", values)}

    def recent_plays(self, limit: int = 50) -> list[dict]:
        """The last plays, newest first, with what was played and where."""
        return [dict(row) for row in self.conn.execute(
            "SELECT p.at, p.source, v.id, v.title, v.rel_path FROM plays p JOIN videos v ON v.id = p.video_id "
            "ORDER BY p.at DESC, p.id DESC LIMIT ?", (limit,))]

    def save_resume(self, video_id: int, position: float) -> None:
        self.conn.execute("UPDATE videos SET resume_pos = ? WHERE id = ?", (position, video_id))
        self.conn.commit()

    # ------------------------------------------------------------ trash

    def mark_trashed(self, video_ids: Iterable[int], item: str, origin: str) -> None:
        now = time.time()
        for vid in video_ids:
            self.conn.execute("UPDATE videos SET trashed = 1, trash_item = ?, trash_origin = ?, trashed_at = ? "
                              "WHERE id = ?", (item, origin, now, vid))
        self.conn.commit()

    def trashed(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, title, path, size, trash_item, trash_origin, trashed_at FROM videos "
                                 "WHERE trashed = 1 ORDER BY trashed_at DESC").fetchall()

    def restore(self, trash_item: str) -> None:
        self.conn.execute("UPDATE videos SET trashed = 0, trash_item = NULL, trash_origin = NULL, trashed_at = NULL "
                          "WHERE trash_item = ?", (trash_item,))
        self.conn.commit()

    def purge(self, trash_item: str) -> list[int]:
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM videos WHERE trash_item = ?", (trash_item,))]
        self.conn.execute("DELETE FROM videos WHERE trash_item = ?", (trash_item,))
        self.cleanup_orphans()
        return ids

    # ------------------------------------------------------------ duplicates

    def dupe_candidates(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, path, size, mtime, duration, width, height, bitrate, is_vr, "
                                 "quick_hash, phash FROM videos WHERE trashed = 0").fetchall()
