"""Moving a performer's scenes into a folder of their own - the real files, not shortcuts.

This is the one part of BlueShelf that changes what is on the disk, so it is careful about it:

  * Every move is written down first, so the whole lot can be put back with one button.
  * The library follows the files, rather than losing sight of them until the next scan.
  * Subtitles and artwork that sit beside a video travel with it.
  * A scene with two performers in it can only live in one folder, so those are pointed out
    rather than quietly filed under whoever was asked for.

The warning it cannot solve: if the files are still seeding in a torrent client, moving them
breaks that. The dialog says so, and the undo log is there for when it turns out to matter.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from blueshelf import paths
from blueshelf.db import Database
from blueshelf.organize import safe_name

# Files that belong to a video and should travel with it.
COMPANIONS = (".nfo", ".srt", ".vtt", ".ass", ".sub", ".jpg", ".jpeg", ".png", ".webp", ".txt")


@dataclass
class Move:
    video_id: int
    source: str
    target: str
    others: list[str] = field(default_factory=list)  # the other performers in this scene

    @property
    def name(self) -> str:
        return os.path.basename(self.source)

    @property
    def shared(self) -> bool:
        return bool(self.others)


@dataclass
class Result:
    moved: list[Move] = field(default_factory=list)
    failed: list[tuple[Move, str]] = field(default_factory=list)
    log: str = ""


def folder_for(root: str, performer: str) -> str:
    """Where a performer's scenes would go by default: a folder of their name in the library."""
    return os.path.join(root, safe_name(performer))


def matching_folders(db: Database, performer: str, depth: int = 3) -> list[str]:
    """Folders already on the disk that look like they belong to this performer.

    Most collections already have some of this done by hand, so the folder someone made months ago
    is a better suggestion than a new one beside it. Spelling differences that BlueShelf already
    treats as the same name - spacing, capitals, doubled letters - match here too.
    """
    from blueshelf.parser import performer_key

    wanted = performer_key(performer)
    if not wanted:
        return []
    found: list[str] = []
    for library in db.libraries():
        root = Path(library["path"])
        for level in range(1, depth + 1):
            try:
                candidates = root.glob("/".join(["*"] * level))
            except OSError:
                continue
            try:
                for candidate in candidates:
                    if candidate.is_dir() and performer_key(candidate.name) == wanted:
                        if str(candidate) not in found:
                            found.append(str(candidate))
            except OSError:
                continue
    return found


def library_root(db: Database, path: str) -> str | None:
    """Which library folder a path sits in, or None when it is outside them all."""
    wanted = os.path.normcase(os.path.abspath(path))
    for library in db.libraries():
        root = os.path.normcase(os.path.abspath(library["path"]))
        if wanted == root or wanted.startswith(root + os.sep):
            return library["path"]
    return None


def companions(source: str) -> list[str]:
    """Subtitle and artwork files named after the video, which belong with it."""
    stem = os.path.splitext(source)[0]
    found: list[str] = []
    seen: set[str] = set()
    for suffix in COMPANIONS:
        for candidate in (stem + suffix, stem + suffix.upper()):
            # Windows does not care about capitals, so ".srt" and ".SRT" are the same file there
            # and must not be counted - or moved - twice.
            key = os.path.normcase(candidate)
            if key not in seen and os.path.isfile(candidate):
                seen.add(key)
                found.append(candidate)
    return found


def unique_target(target: str) -> str:
    """A name nothing else is using, so a move can never write over a file."""
    if not os.path.exists(target):
        return target
    stem, extension = os.path.splitext(target)
    for number in range(2, 1000):
        candidate = f"{stem} ({number}){extension}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem} ({int(time.time())}){extension}"


def plan(db: Database, performer_id: int, destination: str, skip_shared: bool = False) -> list[Move]:
    """What would move, and what else is in each scene. Scenes already there are left alone."""
    performer = next((row["name"] for row in db.performer_counts(True) if row["id"] == performer_id), "")
    wanted = os.path.normcase(os.path.abspath(destination))
    moves: list[Move] = []
    for video in db.list_videos(performer_id=performer_id, show_hidden=True, sort="title", descending=False):
        source = video["path"]
        if os.path.normcase(os.path.abspath(os.path.dirname(source))) == wanted:
            continue  # already in the folder
        others = [name.strip() for name in (video.get("performer_names") or "").split(",")
                  if name.strip() and name.strip() != performer]
        if others and skip_shared:
            continue
        moves.append(Move(video["id"], source, os.path.join(destination, os.path.basename(source)), others))
    return moves


def _shift(source: str, target: str) -> None:
    """Move one file. A rename within a drive is instant; across drives it has to be copied."""
    try:
        os.rename(source, target)
    except OSError:
        shutil.move(source, target)


def apply(db: Database, moves: list[Move], on_progress=None) -> Result:
    """Do the moves, keep the library pointing at the files, and write down how to undo it."""
    result = Result()
    record: list[dict] = []
    for number, move in enumerate(moves, 1):
        if on_progress:
            on_progress(number, len(moves), move.name)
        if not os.path.isfile(move.source):
            result.failed.append((move, "The file is no longer there"))
            continue
        try:
            os.makedirs(os.path.dirname(move.target), exist_ok=True)
            target = unique_target(move.target)
            extras = []
            for companion in companions(move.source):
                spare = unique_target(os.path.join(os.path.dirname(target),
                                                   os.path.splitext(os.path.basename(target))[0]
                                                   + os.path.splitext(companion)[1]))
                try:
                    _shift(companion, spare)
                except OSError:
                    continue  # a stubborn subtitle is no reason to leave the video behind
                extras.append({"from": companion, "to": spare})
            _shift(move.source, target)
        except OSError as error:
            result.failed.append((move, str(error)))
            continue
        moved = Move(move.video_id, move.source, target, move.others)
        result.moved.append(moved)
        record.append({"video": move.video_id, "from": move.source, "to": target, "extras": extras})
        root = library_root(db, target)
        relative = os.path.relpath(target, root) if root else os.path.basename(target)
        db.relocate_video(move.video_id, target, relative)
    if record:
        result.log = write_log(record)
    return result


def log_dir() -> Path:
    folder = paths.data_dir() / "moves"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_log(record: list[dict]) -> str:
    path = log_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"when": time.time(), "moves": record}, indent=1), encoding="utf-8")
    return str(path)


def recent_logs(limit: int = 20) -> list[str]:
    return [str(path) for path in sorted(log_dir().glob("*.json"), reverse=True)[:limit]]


def undo(db: Database, log: str) -> tuple[int, list[str]]:
    """Put a batch of moves back where they came from. Returns how many, and what would not go."""
    try:
        record = json.loads(Path(log).read_text(encoding="utf-8"))["moves"]
    except (OSError, ValueError, KeyError):
        return 0, ["That record of the move could not be read"]
    back, problems = 0, []
    for entry in reversed(record):
        target, source = entry["to"], entry["from"]
        if not os.path.isfile(target):
            problems.append(f"{os.path.basename(target)} is not where it was put")
            continue
        try:
            os.makedirs(os.path.dirname(source), exist_ok=True)
            _shift(target, unique_target(source) if os.path.exists(source) else source)
            for extra in entry.get("extras", []):
                if os.path.isfile(extra["to"]):
                    _shift(extra["to"], extra["from"])
        except OSError as error:
            problems.append(f"{os.path.basename(target)}: {error}")
            continue
        root = library_root(db, source)
        db.relocate_video(entry["video"], source,
                          os.path.relpath(source, root) if root else os.path.basename(source))
        back += 1
    if back:
        try:
            Path(log).unlink()
        except OSError:
            pass
    return back, problems
