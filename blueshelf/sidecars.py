"""Writing what BlueShelf knows about a film into a small file beside it.

So that nothing here is a trap. Somebody who organises a library in BlueShelf can open it in
Kodi, Jellyfin or Emby, hand the folder to somebody else, or come back after reinstalling
everything, and their performers, tags, ratings and marked scenes are still there - because
they are written next to the films in the shape those programs already read.

    film.mp4
    film.nfo     <- this

The format is Kodi's, which the others follow. Scenes are BlueShelf's own addition: other
programs ignore what they do not recognise, and BlueShelf reads them back.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from blueshelf.db import Database

WRITE_SETTING = "write_sidecars"
MARK = "Written by BlueShelf. Safe to delete: it is a copy of what BlueShelf knows, not the only one."


def sidecar_for(film: str | Path) -> Path:
    return Path(film).with_suffix(".nfo")


def _text(parent: ElementTree.Element, tag: str, value) -> None:
    if value in (None, "", 0):
        return
    ElementTree.SubElement(parent, tag).text = str(value)


def as_xml(video: dict, scenes: Iterable[dict] = ()) -> str:
    root = ElementTree.Element("movie")
    root.append(ElementTree.Comment(MARK))
    _text(root, "title", video.get("title") or Path(video["path"]).stem)
    _text(root, "plot", video.get("details"))
    _text(root, "studio", video.get("studio"))
    date = (video.get("release_date") or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", date):
        _text(root, "premiered", date)
        _text(root, "year", date[:4])
    _text(root, "url", video.get("url"))
    # Kodi counts out of ten; BlueShelf out of five.
    if video.get("rating"):
        _text(root, "userrating", int(video["rating"]) * 2)
    if video.get("favorite"):
        _text(root, "isfavorite", "true")
    _text(root, "playcount", video.get("play_count"))
    if video.get("last_played"):
        _text(root, "lastplayed",
              datetime.fromtimestamp(float(video["last_played"]), timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    if video.get("duration"):
        _text(root, "runtime", int(float(video["duration"]) // 60))
    if (video.get("resume_pos") or 0) > 0:
        resume = ElementTree.SubElement(root, "resume")
        _text(resume, "position", round(float(video["resume_pos"]), 3))
        _text(resume, "total", round(float(video.get("duration") or 0), 3))
    for tag in video.get("tags") or []:
        _text(root, "tag", tag)
    for name in video.get("performers") or []:
        actor = ElementTree.SubElement(root, "actor")
        _text(actor, "name", name)
    # Stretches of the film worth finding again. Nothing else reads these; they are here so a
    # person's own marks survive leaving and coming back.
    for scene in scenes:
        element = ElementTree.SubElement(root, "scene")
        element.set("start", f"{float(scene['start']):.3f}")
        element.set("end", f"{float(scene['end']):.3f}")
        element.text = scene.get("label") or ""
    if video.get("is_vr"):
        vr = ElementTree.SubElement(root, "vr")
        for name, value in (("layout", video.get("vr_layout")), ("projection", video.get("vr_proj")),
                            ("fov", video.get("vr_lens_fov"))):
            if value is not None:
                vr.set(name, str(value))
    ElementTree.indent(root, "  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ElementTree.tostring(root, encoding="unicode") + "\n"


def write_one(db: Database, video_id: int) -> Path | None:
    """The file beside one film. Returns where it was written, or None if it could not be."""
    video = db.get_video(video_id)
    if video is None or not video["path"]:
        return None
    beside = sidecar_for(video["path"])
    body = as_xml(video, db.scenes(video_id))
    try:
        if beside.is_file() and beside.read_text(encoding="utf-8", errors="replace") == body:
            return beside          # unchanged: do not touch the file at all
        beside.write_text(body, encoding="utf-8")
        return beside
    except OSError:
        return None                # a read-only disc, or a film that has gone: not worth stopping for


def write_all(db: Database, report: Callable[[str], None] = lambda _m: None,
              stop: Callable[[], bool] = lambda: False) -> dict:
    """One beside every film in the library."""
    rows = db.conn.execute("SELECT id FROM videos WHERE trashed = 0 ORDER BY id").fetchall()
    counted = {"written": 0, "could not": 0}
    for n, row in enumerate(rows, 1):
        if stop():
            break
        if write_one(db, row["id"]):
            counted["written"] += 1
        else:
            counted["could not"] += 1
        if n % 50 == 0 or n == len(rows):
            report(f"Written beside {counted['written']} of {len(rows)} films…")
    report(f"Written beside {counted['written']} films."
           + (f" {counted['could not']} could not be written." if counted["could not"] else ""))
    return counted


def remove_all(db: Database, report: Callable[[str], None] = lambda _m: None) -> int:
    """Take them away again - only the ones BlueShelf wrote, never somebody else's."""
    gone = 0
    for row in db.conn.execute("SELECT path FROM videos WHERE trashed = 0"):
        beside = sidecar_for(row["path"])
        try:
            if beside.is_file() and MARK in beside.read_text(encoding="utf-8", errors="replace"):
                beside.unlink()
                gone += 1
        except OSError:
            continue
    report(f"Removed {gone} of BlueShelf's own files.")
    return gone


def wanted(db: Database) -> bool:
    return bool(db.get_setting(WRITE_SETTING, False))


def keep_up_to_date(db: Database, video_id: int) -> None:
    """Called when something about a film changes. Does nothing unless it was asked for."""
    if wanted(db):
        write_one(db, video_id)
