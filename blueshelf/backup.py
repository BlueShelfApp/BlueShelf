"""Backing up and restoring the library data.

The videos themselves are never touched. This is about the work built on top of them - performers,
categories, ratings, favourites, resume points, thumbnails - which lives only on this PC and is
otherwise lost when Windows is reinstalled or the app is moved to another machine.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import zipfile

from blueshelf import paths

SUFFIX = ".blueshelfbackup.zip"
MANIFEST = "blueshelf-backup.json"
KEEP_AUTOMATIC = 5


def default_name(when: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when or time.time()))
    return f"blueshelf-{stamp}{SUFFIX}"


def _snapshot_database(destination: str) -> None:
    """Copy the database safely even while BlueShelf has it open."""
    source = sqlite3.connect(f"file:{paths.db_path()}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()


def _counts(db_path: str) -> dict:
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        counts = {}
        for name, query in (("videos", "SELECT count(*) FROM videos WHERE trashed = 0"),
                            ("performers", "SELECT count(*) FROM performers"),
                            ("libraries", "SELECT count(*) FROM libraries")):
            try:
                counts[name] = db.execute(query).fetchone()[0]
            except sqlite3.Error:
                counts[name] = 0
        return counts
    finally:
        db.close()


def create(destination: str, include_thumbs: bool = True) -> dict:
    """Write a backup zip. Returns a summary of what went into it."""
    data_dir = paths.data_dir()
    staging = str(data_dir / "backup-staging.db")
    _snapshot_database(staging)
    summary = _counts(staging)
    thumbs = sorted(paths.thumbs_dir().glob("*.jpg")) if include_thumbs else []
    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
    try:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, json.dumps({"created": time.time(), "app": "BlueShelf",
                                                   "thumbs": len(thumbs), **summary}, indent=1))
            archive.write(staging, "library.db")
            for thumb in thumbs:
                archive.write(thumb, f"thumbs/{thumb.name}")
    finally:
        try:
            os.remove(staging)
        except OSError:
            pass
    summary["thumbs"] = len(thumbs)
    summary["path"] = destination
    summary["size"] = os.path.getsize(destination)
    return summary


def inspect(path: str) -> dict | None:
    """What is inside a backup, without unpacking it. None when the file is not a BlueShelf backup."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "library.db" not in names:
                return None
            manifest = json.loads(archive.read(MANIFEST)) if MANIFEST in names else {}
    except (OSError, ValueError, zipfile.BadZipFile):
        return None
    manifest.setdefault("thumbs", sum(1 for n in names if n.startswith("thumbs/")))
    manifest["path"] = path
    return manifest


def restore(path: str) -> dict:
    """Replace the library data with a backup. The data being replaced is kept alongside it.

    BlueShelf must not be holding the database open when this runs, so the app closes its connections
    and restarts around it.
    """
    details = inspect(path)
    if details is None:
        raise ValueError("That file is not a BlueShelf backup.")
    data_dir = paths.data_dir()
    thumbs_dir = paths.thumbs_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    replaced = data_dir / f"replaced-{stamp}"
    replaced.mkdir(parents=True, exist_ok=True)
    for leftover in ("library.db", "library.db-wal", "library.db-shm"):
        current = data_dir / leftover
        if current.exists():
            shutil.move(str(current), str(replaced / leftover))
    if thumbs_dir.exists() and any(thumbs_dir.iterdir()):
        shutil.move(str(thumbs_dir), str(replaced / "thumbs"))
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path) as archive:
        with archive.open("library.db") as source, open(data_dir / "library.db", "wb") as target:
            shutil.copyfileobj(source, target)
        for name in archive.namelist():
            if name.startswith("thumbs/") and not name.endswith("/"):
                with archive.open(name) as source, open(thumbs_dir / os.path.basename(name), "wb") as target:
                    shutil.copyfileobj(source, target)
    details["replaced_data_kept_in"] = str(replaced)
    return details


def automatic_dir() -> str:
    folder = paths.data_dir() / "backups"
    folder.mkdir(exist_ok=True)
    return str(folder)


def run_automatic(every_days: float = 7.0) -> dict | None:
    """Make a backup if the newest one is older than every_days. Keeps the most recent few."""
    folder = automatic_dir()
    existing = sorted((os.path.join(folder, name) for name in os.listdir(folder) if name.endswith(SUFFIX)),
                      key=os.path.getmtime)
    if existing and time.time() - os.path.getmtime(existing[-1]) < every_days * 86400:
        return None
    fresh = os.path.join(folder, default_name())
    summary = create(fresh)
    # Never prune the backup just written: its name can collide with an older one made in the same second.
    stale = [path for path in existing if os.path.abspath(path) != os.path.abspath(fresh)]
    for old in stale[:max(0, len(stale) + 1 - KEEP_AUTOMATIC)]:
        try:
            os.remove(old)
        except OSError:
            pass
    return summary
