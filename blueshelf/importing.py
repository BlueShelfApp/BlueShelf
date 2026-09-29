"""Bringing somebody's existing library in, without asking them to redo any of it.

People arrive with years of work in Stash, XBVR, Kodi or Jellyfin - performers named, tags
put on, ratings given, scenes marked. None of that is in the files, so moving to another
program normally means losing it or typing it all again. This reads it out of whatever they
were using and matches it to their films.

Nothing here touches their videos. Files are never moved, renamed or written to; the import
only adds what it learned to BlueShelf's own library, and it can be run again safely.

    incoming = read_stash(r"C:\\Users\\me\\.stash\\stash-go.sqlite")
    plan = Match(db).plan(incoming)          # what lines up, what does not, and why
    plan.remap("D:\\videos", r"\\\\nas\\films")   # their drive letters were not ours
    apply_plan(db, plan)
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from blueshelf.dupes import quick_hash
from blueshelf.db import Database
from blueshelf.parser import parse_path

# A marked point with no end - Stash markers often have none - becomes a stretch this long,
# which the person can trim afterwards. Better than throwing the mark away.
POINT_SCENE = 60.0
VIDEO_SUFFIXES = {".mp4", ".mkv", ".m4v", ".mov", ".avi", ".wmv", ".webm", ".ts", ".m2ts", ".mpg", ".mpeg"}


@dataclass
class Incoming:
    """One film as another program had it. Everything but ``path`` is optional: a reader fills
    in what its source knows, and ``None`` means "it had nothing to say", which is different
    from "it said empty" and is why nothing is overwritten with a blank."""

    path: str
    size: int | None = None                   # what the other program recorded, if it did
    title: str | None = None
    studio: str | None = None
    date: str | None = None
    details: str | None = None
    url: str | None = None
    performers: list[str] | None = None
    tags: list[str] | None = None
    rating: int | None = None                 # 0-5, as BlueShelf counts them
    favorite: bool | None = None
    play_count: int | None = None
    last_played: float | None = None
    resume: float | None = None
    scenes: list[tuple[float, float, str]] = field(default_factory=list)
    source: str = ""

    def something(self) -> bool:
        """Whether this is worth importing at all - a bare path teaches nothing."""
        return any(v not in (None, "", [], 0) for v in
                   (self.title, self.studio, self.date, self.details, self.url, self.performers,
                    self.tags, self.rating, self.favorite, self.play_count, self.last_played,
                    self.resume, self.scenes))


# --------------------------------------------------------------------------- reading a database

def _open_copy(path: str | Path) -> sqlite3.Connection:
    """A read-only copy of somebody's database, so a running program cannot be disturbed.

    Opening theirs in place would fail while their program holds it, and could leave a stray
    journal beside it. The copy takes the write-ahead log with it or recent changes are missing.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"There is no database at {source}")
    folder = Path(tempfile.mkdtemp(prefix="blueshelf-import-"))
    copy = folder / source.name
    shutil.copy2(source, copy)
    for extra in ("-wal", "-shm"):
        beside = source.with_name(source.name + extra)
        if beside.is_file():
            shutil.copy2(beside, copy.with_name(copy.name + extra))
    conn = sqlite3.connect(str(copy))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _first(columns: set[str], *names: str) -> str | None:
    """The first of these column names the table actually has - programs rename things
    between versions, and an import that only works on this month's version is no use."""
    return next((name for name in names if name in columns), None)


def _when(value) -> float | None:
    """A time as seconds since 1970, from whatever the other program stored."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 1e6 else None
    text = str(value).strip().replace("Z", "+00:00")
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            when = datetime.strptime(text[:19], shape)
            return when.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _date(value) -> str | None:
    """A release date as YYYY-MM-DD."""
    if not value:
        return None
    text = str(value).strip()
    found = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if found:
        return found.group(0)
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01"
    when = _when(text)
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d") if when else None


def _stars(rating, out_of: int = 5) -> int | None:
    """A rating as BlueShelf counts them, nought to five.

    Every program counts differently - Stash out of a hundred (or five, before), Kodi out of
    ten, XBVR out of five with halves - so the reader says which, rather than this guessing
    and turning somebody's four stars into nothing.
    """
    if rating in (None, "", 0):
        return None
    try:
        value = float(rating)
    except (TypeError, ValueError):
        return None
    if value > out_of:            # a scale we were told wrong: take it at face value
        out_of = 100 if value > 10 else 10
    return max(1, min(5, round(value * 5.0 / out_of)))


# --------------------------------------------------------------------------- Stash

def read_stash(database: str | Path, report: Callable[[str], None] = lambda _m: None) -> list[Incoming]:
    """Everything Stash knows about each film in its library.

    Stash changed where it keeps a film's path - older versions on the scene itself, newer
    ones in a files-and-folders table a scene points at - so both are read. Its scene markers
    become BlueShelf scenes, which is what they are: a stretch of a film worth finding again.
    """
    conn = _open_copy(database)
    try:
        tables = _tables(conn)
        if "scenes" not in tables:
            raise ValueError("That does not look like a Stash database - it has no scenes.")
        scene_columns = _columns(conn, "scenes")
        rating_column = _first(scene_columns, "rating100", "rating")
        out_of = 100 if rating_column == "rating100" else 5

        paths: dict[int, tuple[str, int | None]] = {}
        if "path" in scene_columns:                       # Stash before it split files out
            has_size = "size" in scene_columns
            for row in conn.execute(
                    f"SELECT id, path{', size' if has_size else ''} FROM scenes WHERE path IS NOT NULL"):
                paths[row["id"]] = (row["path"], int(row["size"]) if has_size and row["size"] else None)
        elif {"scenes_files", "files", "folders"} <= tables:
            for row in conn.execute(
                    "SELECT sf.scene_id AS id, fo.path AS folder, fi.basename AS name, fi.size AS size "
                    "FROM scenes_files sf JOIN files fi ON fi.id = sf.file_id "
                    "JOIN folders fo ON fo.id = fi.parent_folder_id "
                    "ORDER BY sf.scene_id"):
                # The first file of a scene, or the one marked primary if this version says so.
                paths.setdefault(row["id"], (os.path.join(row["folder"], row["name"]),
                                             int(row["size"]) if row["size"] else None))
        if not paths:
            raise ValueError("No film paths could be read out of that Stash database.")

        wanted = ["id"]
        for name in ("title", "details", "date", "studio_id", "play_count", "resume_time",
                     "last_played_at", "organized", "o_counter", "url"):
            if name in scene_columns:
                wanted.append(name)
        if rating_column:
            wanted.append(rating_column)
        rows = conn.execute(f"SELECT {', '.join(wanted)} FROM scenes").fetchall()

        studios = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM studios")} \
            if "studios" in tables else {}
        performers = _linked(conn, tables, "performers", "performers_scenes", "performer_id")
        tags = _linked(conn, tables, "tags", "scenes_tags", "tag_id")
        urls: dict[int, str] = {}
        if "scenes_urls" in tables:
            for row in conn.execute("SELECT scene_id, url FROM scenes_urls ORDER BY scene_id, position"):
                urls.setdefault(row["scene_id"], row["url"])
        marks = _stash_markers(conn, tables)

        found = []
        for row in rows:
            keys = row.keys()
            where = paths.get(row["id"])
            if not where:
                continue
            path, size = where
            found.append(Incoming(
                path=path,
                size=size,
                title=(row["title"] or None) if "title" in keys else None,
                studio=studios.get(row["studio_id"]) if "studio_id" in keys else None,
                date=_date(row["date"]) if "date" in keys else None,
                details=(row["details"] or None) if "details" in keys else None,
                url=urls.get(row["id"]) or ((row["url"] or None) if "url" in keys else None),
                performers=performers.get(row["id"]),
                tags=tags.get(row["id"]),
                rating=_stars(row[rating_column], out_of) if rating_column else None,
                play_count=int(row["play_count"] or 0) or None if "play_count" in keys else None,
                last_played=_when(row["last_played_at"]) if "last_played_at" in keys else None,
                resume=float(row["resume_time"] or 0) or None if "resume_time" in keys else None,
                scenes=marks.get(row["id"], []),
                source="Stash"))
        report(f"Read {len(found)} films from Stash.")
        return found
    finally:
        conn.close()


def _linked(conn: sqlite3.Connection, tables: set[str], names_table: str,
            join_table: str, column: str) -> dict[int, list[str]]:
    """Names joined to scenes - performers, tags - as {scene id: [name, …]}."""
    if not {names_table, join_table} <= tables:
        return {}
    out: dict[int, list[str]] = {}
    for row in conn.execute(f"SELECT j.scene_id AS scene, n.name AS name FROM {join_table} j "
                            f"JOIN {names_table} n ON n.id = j.{column} ORDER BY n.name"):
        if row["name"]:
            out.setdefault(row["scene"], []).append(row["name"])
    return out


def _stash_markers(conn: sqlite3.Connection, tables: set[str]) -> dict[int, list[tuple[float, float, str]]]:
    """Stash's scene markers, which are BlueShelf's scenes under another name."""
    if "scene_markers" not in tables:
        return {}
    columns = _columns(conn, "scene_markers")
    end = "end_seconds" if "end_seconds" in columns else None
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM tags")} if "tags" in tables else {}
    out: dict[int, list[tuple[float, float, str]]] = {}
    fields = "scene_id, seconds, title" + (", primary_tag_id" if "primary_tag_id" in columns else "") \
        + (f", {end}" if end else "")
    for row in conn.execute(f"SELECT {fields} FROM scene_markers"):
        keys = row.keys()
        start = float(row["seconds"] or 0)
        stop = float(row[end]) if end and row[end] else start + POINT_SCENE
        label = (row["title"] or "").strip()
        if not label and "primary_tag_id" in keys:
            label = names.get(row["primary_tag_id"], "")
        out.setdefault(row["scene_id"], []).append((start, max(stop, start + 2.0), label))
    return out


# --------------------------------------------------------------------------- XBVR

def read_xbvr(database: str | Path, report: Callable[[str], None] = lambda _m: None) -> list[Incoming]:
    """XBVR's library: the VR one, which knows the site a film came from and who is in it."""
    conn = _open_copy(database)
    try:
        tables = _tables(conn)
        if "scenes" not in tables or "files" not in tables:
            raise ValueError("That does not look like an XBVR database.")
        columns = _columns(conn, "scenes")
        file_columns = _columns(conn, "files")
        path_column = _first(file_columns, "path")
        name_column = _first(file_columns, "filename")
        if not path_column or "scene_id" not in file_columns:
            raise ValueError("XBVR's files table is not laid out as expected.")
        paths: dict[int, tuple[str, int | None]] = {}
        has_size = "size" in file_columns
        for row in conn.execute(f"SELECT scene_id, {path_column} AS folder, {name_column} AS name"
                                f"{', size' if has_size else ''} FROM files"):
            if row["scene_id"]:
                paths.setdefault(row["scene_id"], (os.path.join(row["folder"] or "", row["name"] or ""),
                                                   int(row["size"]) if has_size and row["size"] else None))

        wanted = ["id"] + [c for c in ("title", "studio", "site", "scene_url", "synopsis", "release_date",
                                       "star_rating", "favourite", "is_watched", "watchlist") if c in columns]
        rows = conn.execute(f"SELECT {', '.join(wanted)} FROM scenes").fetchall()
        casts = _xbvr_linked(conn, tables, "casts", "scene_cast", "cast_id")
        tags = _xbvr_linked(conn, tables, "tags", "scene_tags", "tag_id")

        found = []
        for row in rows:
            keys = row.keys()
            where = paths.get(row["id"])
            if not where:
                continue
            path, size = where
            watched = bool(row["is_watched"]) if "is_watched" in keys else False
            found.append(Incoming(
                path=path,
                size=size,
                title=(row["title"] or None) if "title" in keys else None,
                # XBVR keeps both: the studio that made it and the site it came from.
                studio=((row["studio"] if "studio" in keys else None)
                        or (row["site"] if "site" in keys else None) or None),
                date=_date(row["release_date"]) if "release_date" in keys else None,
                details=(row["synopsis"] or None) if "synopsis" in keys else None,
                url=(row["scene_url"] or None) if "scene_url" in keys else None,
                performers=casts.get(row["id"]),
                tags=tags.get(row["id"]),
                rating=_stars(row["star_rating"]) if "star_rating" in keys else None,
                favorite=bool(row["favourite"]) if "favourite" in keys else None,
                play_count=1 if watched else None,
                source="XBVR"))
        report(f"Read {len(found)} films from XBVR.")
        return found
    finally:
        conn.close()


def _xbvr_linked(conn: sqlite3.Connection, tables: set[str], names_table: str,
                 join_table: str, column: str) -> dict[int, list[str]]:
    if not {names_table, join_table} <= tables:
        return {}
    out: dict[int, list[str]] = {}
    for row in conn.execute(f"SELECT j.scene_id AS scene, n.name AS name FROM {join_table} j "
                            f"JOIN {names_table} n ON n.id = j.{column} ORDER BY n.name"):
        if row["name"]:
            out.setdefault(row["scene"], []).append(row["name"])
    return out


# --------------------------------------------------------------------------- NFO files beside films

# What Kodi, Jellyfin, Emby and the scrapers that feed them write next to a film.
def read_nfo_file(nfo: str | Path, film: str | Path | None = None) -> Incoming | None:
    try:
        root = ElementTree.parse(str(nfo)).getroot()
    except (ElementTree.ParseError, OSError):
        return None
    text = lambda tag: (root.findtext(tag) or "").strip()  # noqa: E731
    performers = [(actor.findtext("name") or "").strip() for actor in root.findall("actor")]
    tags = [t.strip() for element in ("tag", "genre") for t in
            [(node.text or "") for node in root.findall(element)] if t.strip()]
    rating = text("userrating") or text("rating")
    watched = text("playcount")
    date = text("premiered") or text("aired") or text("releasedate") or text("year")
    resume = root.findtext("resume/position") or ""
    # Scenes, if BlueShelf wrote this file: a person's own marks, coming home.
    scenes = []
    for element in root.findall("scene"):
        try:
            start, end = float(element.get("start", "")), float(element.get("end", ""))
        except ValueError:
            continue
        if end > start:
            scenes.append((start, end, (element.text or "").strip()))
    return Incoming(
        scenes=scenes,
        path=str(film) if film else "",
        title=text("title") or text("originaltitle") or None,
        studio=text("studio") or None,
        date=_date(date),
        details=text("plot") or text("outline") or None,
        url=text("url") or None,
        performers=[p for p in performers if p] or None,
        tags=tags or None,
        rating=_stars(rating, 10),
        favorite=True if (root.findtext("isfavorite") or "").strip().lower() in ("true", "1") else None,
        play_count=int(watched) if watched.isdigit() and int(watched) else None,
        last_played=_when(text("lastplayed")),
        resume=float(resume) if resume.replace(".", "", 1).isdigit() and float(resume) > 0 else None,
        source="NFO")


def read_nfos(folders: Iterable[str | Path], report: Callable[[str], None] = lambda _m: None) -> list[Incoming]:
    """Every film under these folders that has an .nfo beside it.

    Kodi and the rest look for ``film.nfo`` next to ``film.mp4`` - and, in a folder holding one
    film, ``movie.nfo``.
    """
    found = []
    for folder in folders:
        for here, _folders, names in os.walk(str(folder)):
            films = [n for n in names if Path(n).suffix.lower() in VIDEO_SUFFIXES]
            nfos = {n.lower() for n in names if n.lower().endswith(".nfo")}
            for name in films:
                film = Path(here) / name
                beside = f"{Path(name).stem}.nfo".lower()
                which = beside if beside in nfos else ("movie.nfo" if "movie.nfo" in nfos and len(films) == 1 else None)
                if not which:
                    continue
                real = next(n for n in names if n.lower() == which)
                one = read_nfo_file(Path(here) / real, film)
                if one and one.something():
                    found.append(one)
    report(f"Read {len(found)} films from NFO files.")
    return found


# --------------------------------------------------------------------------- folders and filenames

def read_folders(folders: Iterable[str | Path], report: Callable[[str], None] = lambda _m: None) -> list[Incoming]:
    """What the names themselves say, for a library kept in no program at all.

    The same reading BlueShelf does when it scans, offered as an import so it can be looked
    over first and so it can be pointed at films that are already in the library.
    """
    found = []
    for folder in folders:
        root = Path(folder)
        for here, _folders, names in os.walk(str(root)):
            for name in names:
                if Path(name).suffix.lower() not in VIDEO_SUFFIXES:
                    continue
                film = Path(here) / name
                try:
                    relative = film.relative_to(root)
                except ValueError:
                    relative = Path(name)
                alone = sum(1 for n in names if Path(n).suffix.lower() in VIDEO_SUFFIXES) == 1
                parsed = parse_path(str(relative), alone)
                found.append(Incoming(
                    path=str(film),
                    title=parsed.title or None,
                    studio=parsed.studio or None,
                    date=_date(parsed.date),
                    performers=list(parsed.performers) or None,
                    tags=list(parsed.tags) or None,
                    source="Folders"))
    report(f"Read {len(found)} films from folder and file names.")
    return found


# --------------------------------------------------------------------------- matching to our films

def _tidy(path: str) -> str:
    return str(path or "").replace("/", "\\").rstrip("\\").lower()


def _parts(path: str) -> list[str]:
    return [p for p in _tidy(path).split("\\") if p]


@dataclass
class Plan:
    """What would happen, before anything does."""

    matched: list[tuple[Incoming, int]] = field(default_factory=list)   # (theirs, our video id)
    missing: list[Incoming] = field(default_factory=list)
    nothing_to_learn: list[Incoming] = field(default_factory=list)
    how: dict[str, int] = field(default_factory=dict)                   # by path, by name and size, …

    def summary(self) -> str:
        ways = ", ".join(f"{n} {way}" for way, n in sorted(self.how.items(), key=lambda x: -x[1]))
        parts = [f"{len(self.matched)} of {len(self.matched) + len(self.missing)} films matched"]
        if ways:
            parts.append(f"({ways})")
        if self.missing:
            parts.append(f"· {len(self.missing)} not found here")
        if self.nothing_to_learn:
            parts.append(f"· {len(self.nothing_to_learn)} had nothing to add")
        return " ".join(parts)


class Match:
    """Lining somebody else's paths up with the films in this library.

    Three ways, tried in turn: the path itself, the file's name and size, and a quick hash of
    the file's own bytes. The last one finds a film that was moved, renamed or copied to
    another drive, which is the usual reason an import comes up empty.
    """

    def __init__(self, db: Database):
        self.db = db
        self.roots: list[tuple[str, str]] = []
        self._ours: list[dict] = [dict(r) for r in db.conn.execute(
            "SELECT id, path, size, quick_hash FROM videos WHERE trashed = 0")]
        self._by_path = {_tidy(r["path"]): r["id"] for r in self._ours}
        self._by_name: dict[tuple[str, int], list[int]] = {}
        for row in self._ours:
            self._by_name.setdefault((Path(row["path"]).name.lower(), row["size"]), []).append(row["id"])
        self._by_hash: dict[str, int] = {}
        self._hashed = False

    def remap(self, theirs: str, ours: str) -> None:
        """Their folder is our folder: D:\\videos was where they kept what is on our NAS."""
        self.roots.append((_tidy(theirs), str(ours)))

    def _moved(self, path: str) -> str:
        low = _tidy(path)
        for theirs, ours in self.roots:
            if low == theirs or low.startswith(theirs + "\\"):
                return str(Path(ours) / path[len(theirs):].lstrip("\\/"))
        return path

    def _hashes(self, report: Callable[[str], None]) -> dict[str, int]:
        """Hashes of our own films, worked out once and remembered in the library."""
        if self._hashed:
            return self._by_hash
        self._hashed = True
        missing = [r for r in self._ours if not r["quick_hash"]]
        for n, row in enumerate(missing, 1):
            if n % 25 == 0 or n == len(missing):
                report(f"Looking at the films themselves… {n} of {len(missing)}")
            made = quick_hash(row["path"], row["size"])
            if made:
                row["quick_hash"] = made
                self.db.set_fields(row["id"], quick_hash=made)
        for row in self._ours:
            if row["quick_hash"]:
                self._by_hash.setdefault(row["quick_hash"], row["id"])
        return self._by_hash

    def plan(self, incoming: list[Incoming], by_content: bool = True,
             report: Callable[[str], None] = lambda _m: None) -> Plan:
        out = Plan()
        unmatched: list[Incoming] = []
        for one in incoming:
            if not one.something():
                out.nothing_to_learn.append(one)
                continue
            path = self._moved(one.path)
            found = self._by_path.get(_tidy(path))
            if found:
                out.matched.append((one, found))
                out.how["by path"] = out.how.get("by path", 0) + 1
                continue
            unmatched.append(one)

        still: list[Incoming] = []
        for one in unmatched:
            name = Path(self._moved(one.path)).name.lower()
            if one.size:
                # The size as well: two films called "scene 1.mp4" in different folders are not
                # the same film, and quietly putting one's tags on the other would be worse than
                # not matching at all.
                ids = self._by_name.get((name, one.size), [])
                way = "by name and size"
            else:
                ids = [i for key, found in self._by_name.items() if key[0] == name for i in found]
                way = "by name"
            if len(ids) == 1:
                out.matched.append((one, ids[0]))
                out.how[way] = out.how.get(way, 0) + 1
            else:
                still.append(one)

        if by_content and still:
            report(f"{len(still)} films did not line up by name. Looking at the films themselves…")
            hashes = self._hashes(report)
            for one in still:
                made = quick_hash(one.path, os.path.getsize(one.path)) if os.path.isfile(one.path) else None
                found = hashes.get(made) if made else None
                if found:
                    out.matched.append((one, found))
                    out.how["by the film itself"] = out.how.get("by the film itself", 0) + 1
                else:
                    out.missing.append(one)
        else:
            out.missing.extend(still)
        return out

    def suggest_roots(self, missing: list[Incoming], most: int = 6) -> list[tuple[str, str, int]]:
        """Where their folders line up with ours, worked out from the films that did not match.

        A path of theirs ending the same way as one of ours - the same studio folder and the
        same file name - says the folders above it are the same place under another name.
        """
        ours: dict[tuple[str, ...], str] = {}
        for row in self._ours:
            bits = _parts(row["path"])
            for depth in (1, 2, 3):
                if len(bits) > depth:
                    ours.setdefault(tuple(bits[-depth:]), str(Path(row["path"]).parents[depth - 1]))
        votes: dict[tuple[str, str], int] = {}
        for one in missing:
            bits = _parts(one.path)
            for depth in (3, 2, 1):
                if len(bits) <= depth:
                    continue
                theirs = ours.get(tuple(bits[-depth:]))
                if theirs:
                    mine = str(Path(one.path).parents[depth - 1])
                    votes[(mine, theirs)] = votes.get((mine, theirs), 0) + 1
                    break
        best = sorted(votes.items(), key=lambda x: -x[1])[:most]
        return [(theirs, ours_, count) for (theirs, ours_), count in best]


# --------------------------------------------------------------------------- putting it in

def apply_plan(db: Database, plan: Plan, overwrite: bool = False,
               report: Callable[[str], None] = lambda _m: None) -> dict:
    """Add what was learned. Never removes anything, and can be run again.

    ``overwrite`` decides what happens where BlueShelf already has something of its own: off,
    an existing title or rating is left alone and only empty fields are filled; on, theirs
    wins. Performers and tags are always added to rather than replaced - nobody wants an
    import to quietly drop work they did here.
    """
    counted = {"films": 0, "performers": 0, "tags": 0, "scenes": 0, "ratings": 0}
    for one, video_id in plan.matched:
        video = db.get_video(video_id)
        if video is None:
            continue
        fields: dict[str, object] = {}
        # A title, studio or date already here came either from this person - who typed it, and
        # whose work must not be undone - or from reading the file's name, which is a guess and
        # is exactly what they are importing something better than. meta_locked tells them apart.
        theirs_by_hand = bool(video["meta_locked"]) and not overwrite

        def maybe(name: str, value, empty=("", 0, None)) -> None:
            if value in (None, ""):
                return
            if theirs_by_hand and video.get(name) not in empty:
                return
            fields[name] = value

        maybe("title", one.title)
        maybe("studio", one.studio)
        maybe("release_date", one.date)
        maybe("details", one.details)
        maybe("url", one.url)
        if one.rating and (overwrite or not video["rating"]):
            fields["rating"] = one.rating
            counted["ratings"] += 1
        if one.favorite and not video["favorite"]:
            fields["favorite"] = 1
        if one.play_count and one.play_count > (video["play_count"] or 0):
            fields["play_count"] = one.play_count
        if one.last_played and one.last_played > (video["last_played"] or 0):
            fields["last_played"] = one.last_played
        if one.resume and one.resume > 30 and not video["resume_pos"]:
            fields["resume_pos"] = one.resume
        # Locked, so the next scan does not parse the file name over the top of what they
        # spent years getting right.
        if one.title or one.studio or one.date:
            fields["meta_locked"] = 1
        if one.performers:
            fields["perf_locked"] = 1
        if one.tags:
            fields["tags_locked"] = 1
        if fields:
            db.set_fields(video_id, **fields)

        if one.performers:
            have = {p.lower() for p in db.video_performer_names(video_id)}
            for name in one.performers:
                if name.lower() not in have:
                    db.link_performer(video_id, db.performer_id(name))
                    counted["performers"] += 1
        if one.tags:
            have = {t.lower() for t in (video.get("tags") or [])}
            for name in one.tags:
                if name.lower() not in have:
                    db.link_tag(video_id, db.tag_id(name))
                    counted["tags"] += 1
        if one.scenes:
            already = [(s["start"], s["end"]) for s in db.scenes(video_id)]
            for start, end, label in one.scenes:
                if any(abs(start - a) < 1 and abs(end - b) < 1 for a, b in already):
                    continue          # the same mark again: an import run twice
                try:
                    db.add_scene(video_id, start, end, label)
                    counted["scenes"] += 1
                except ValueError:
                    pass              # too short to be a scene here
        counted["films"] += 1
        if counted["films"] % 50 == 0:
            report(f"Brought in {counted['films']} of {len(plan.matched)}…")
    db.commit()
    report(f"Brought in {counted['films']} films, {counted['performers']} performer links, "
           f"{counted['tags']} tags, {counted['scenes']} scenes.")
    return counted


# --------------------------------------------------------------------------- finding their library

def likely_places() -> list[tuple[str, str]]:
    """Where these programs usually keep their library, for offering rather than asking."""
    home = Path.home()
    found = []
    for name, where in (
            ("Stash", home / ".stash" / "stash-go.sqlite"),
            ("Stash", Path(os.environ.get("APPDATA", "")) / "stash" / "stash-go.sqlite"),
            ("XBVR", home / ".config" / "xbvr" / "main.db"),
            ("XBVR", Path(os.environ.get("APPDATA", "")) / "xbvr" / "main.db"),
            ("XBVR", Path(os.environ.get("LOCALAPPDATA", "")) / "xbvr" / "main.db"),
    ):
        try:
            if where.is_file():
                found.append((name, str(where)))
        except OSError:
            continue
    return found
