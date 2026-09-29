"""Background library scanning: discovery, filename parsing, probing and thumbnails."""
from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QThread, Signal

from blueshelf import paths
from blueshelf.db import Database
from blueshelf.parser import (DEFAULT_TAG_RULES, LAYOUT_MONO, LAYOUT_SBS, LAYOUT_TB, PROJ_EQUI180, PROJ_EQUI360,
                          PROJ_FISHEYE, VIDEO_EXTS, parse_path, parse_tag_rules, similar_names, text_ngrams)
from blueshelf.trash import TRASH_DIR_NAME

SKIP_DIRS = {TRASH_DIR_NAME, "$recycle.bin", "system volume information", "@eadir", ".@__thumb"}
SAMPLE_MAX_BYTES = 150 * 1024 * 1024


def discover(root: str):
    """Yield (path, rel_path, size, mtime) for every video file under root."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in SKIP_DIRS and not entry.name.startswith("."):
                        stack.append(entry.path)
                    continue
                if os.path.splitext(entry.name)[1].lower() not in VIDEO_EXTS:
                    continue
                st = entry.stat()
                if "sample" in entry.name.lower() and st.st_size < SAMPLE_MAX_BYTES:
                    continue
                yield entry.path, os.path.relpath(entry.path, root), st.st_size, st.st_mtime
            except OSError:
                continue


def probe(path: str) -> dict | None:
    try:
        result = paths.run_tool([paths.tool("ffprobe"), "-v", "error", "-print_format", "json",
                                 "-show_format", "-show_streams", "-select_streams", "v:0", path], timeout=90)
        info = json.loads(result.stdout or b"{}")
    except Exception:
        return None
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    if not streams:
        return None
    s = streams[0]

    def num(value, cast=float):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    fps = None
    rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or ""
    if "/" in rate:
        n, d = rate.split("/", 1)
        if num(d) and num(n) is not None:
            fps = num(n) / num(d)
    return {
        "duration": num(fmt.get("duration")) or num(s.get("duration")),
        "width": num(s.get("width"), int),
        "height": num(s.get("height"), int),
        "vcodec": s.get("codec_name"),
        "bitrate": num(fmt.get("bit_rate"), int),
        "fps": round(fps, 3) if fps else None,
    }


def vr_flat_filter(layout: int, proj: int, lens_fov: float, width: int, height: int) -> str:
    """ffmpeg filter chain that turns one eye of a VR frame into a normal flat picture."""
    crop = {LAYOUT_SBS: "crop=iw/2:ih:0:0,", LAYOUT_TB: "crop=iw:ih/2:0:0,", LAYOUT_MONO: ""}.get(layout, "")
    if proj == PROJ_FISHEYE:
        source = f"input=fisheye:ih_fov={lens_fov:g}:iv_fov={lens_fov:g}"
    elif proj == PROJ_EQUI360:
        source = "input=equirect"
    else:
        source = "input=hequirect"
    return f"{crop}scale=2048:-2,v360={source}:output=flat:h_fov=100:v_fov=62:w={width}:h={height}"


def make_thumb(video: dict, out_path: str, position: float) -> bool:
    duration = video.get("duration") or 0
    if video.get("is_vr"):
        vf = vr_flat_filter(video["vr_layout"], video["vr_proj"], video["vr_lens_fov"], 480, 270)
    else:
        vf = "scale=480:-2"
    for t in ([duration * position, duration * 0.1, 5.0] if duration else [5.0, 0.0]):
        try:
            paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{max(t, 0):.2f}",
                            "-i", video["path"], "-frames:v", "1", "-vf", vf, "-q:v", "4", out_path], timeout=120)
        except Exception:
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
    return False


def link_known_performers(db: Database, rows=None) -> None:
    """Link performers already in the library whose names appear in a video's path."""
    known = db.known_performers()
    if not known:
        return
    for row in (rows if rows is not None else db.linkable_videos()):
        hits = text_ngrams(row["rel_path"]) & known.keys()
        if not hits:
            continue
        current = db.video_performer_names(row["id"])
        for key in sorted(hits):
            performer_id, name = known[key]
            if any(similar_names(name, other) for other in current):
                continue
            db.link_performer(row["id"], performer_id)
            current.append(name)
    db.commit()


def reparse_videos(db: Database, video_ids: list[int]) -> None:
    """Re-read metadata from file names, discarding manual edits for these videos."""
    hide = db.get_setting("hide_non_adult", True)
    rules = parse_tag_rules(db.get_setting("tag_rules", DEFAULT_TAG_RULES))
    rows = []
    for vid in video_ids:
        video = db.get_video(vid)
        if not video:
            continue
        folder = os.path.dirname(video["rel_path"])
        single = bool(folder) and db.folder_video_count(video["library_id"], folder) == 1
        db.apply_parsed(vid, parse_path(video["rel_path"], single, rules), hide, force=True)
        rows.append({"id": vid, "rel_path": video["rel_path"]})
    db.commit()
    link_known_performers(db, rows)
    db.cleanup_orphans()


class ScanThread(QThread):
    progress = Signal(str, int, int)
    library_changed = Signal()
    thumb_ready = Signal(int, str)
    done = Signal(str)

    def __init__(self, library_ids: list[int] | None = None, reparse: bool = False, parent=None):
        super().__init__(parent)
        self.library_ids = library_ids
        self.reparse = reparse
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        db = Database(str(paths.db_path()))
        try:
            summary = self._scan(db)
        except Exception as exc:
            summary = f"Scan failed: {exc}"
        finally:
            db.close()
        self.done.emit(summary)

    def _scan(self, db: Database) -> str:
        hide = db.get_setting("hide_non_adult", True)
        rules = parse_tag_rules(db.get_setting("tag_rules", DEFAULT_TAG_RULES))
        position = db.get_setting("thumb_position", 0.3)
        added = changed = removed = 0
        unreachable = []

        for lib in db.libraries():
            if self.library_ids is not None and lib["id"] not in self.library_ids:
                continue
            root = lib["path"]
            if not os.path.isdir(root):
                unreachable.append(root)
                continue
            self.progress.emit(f"Looking for videos in {root}", 0, 0)
            existing = db.existing_files(lib["id"])
            found = []
            for item in discover(root):
                found.append(item)
                if len(found) % 50 == 0:
                    self.progress.emit(f"Found {len(found)} videos in {root}", 0, 0)
                if self._stop:
                    return "Scan cancelled"
            per_dir = Counter(os.path.dirname(rel) for _, rel, _, _ in found)
            seen = set()
            for i, (path, rel, size, mtime) in enumerate(found):
                seen.add(path)
                folder = os.path.dirname(rel)
                single = bool(folder) and per_dir[folder] == 1
                row = existing.get(path)
                if row is None:
                    db.insert_video(lib["id"], path, rel, size, mtime, parse_path(rel, single, rules), hide)
                    added += 1
                elif row["size"] != size or abs(row["mtime"] - mtime) > 2:
                    db.file_changed(row["id"], rel, size, mtime)
                    db.apply_parsed(row["id"], parse_path(rel, single, rules), hide)
                    changed += 1
                elif self.reparse:
                    db.apply_parsed(row["id"], parse_path(rel, single, rules), hide)
                if i % 100 == 0:
                    db.commit()
                    self.progress.emit("Reading file names", i, len(found))
            gone = [row["id"] for path, row in existing.items() if path not in seen]
            for vid in gone:
                remove_thumb(vid)
            db.delete_videos(gone)
            removed += len(gone)
            db.commit()

        link_known_performers(db)
        db.cleanup_orphans()
        self.library_changed.emit()

        self._probe_all(db)
        self.library_changed.emit()
        self._thumb_all(db, position)
        self.library_changed.emit()

        parts = [f"{added} new", f"{changed} changed", f"{removed} gone"]
        if unreachable:
            parts.append("could not reach " + ", ".join(unreachable))
        return "Library up to date: " + ", ".join(parts)

    def _probe_all(self, db: Database) -> None:
        rows = db.unprobed()
        if not rows:
            return
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(probe, r["path"]): r for r in rows}
            for n, future in enumerate(as_completed(futures), 1):
                if self._stop:
                    pool.shutdown(cancel_futures=True)
                    return
                row = futures[future]
                info = future.result() or {}
                fields = {k: v for k, v in info.items() if v is not None}
                w, h = info.get("width") or 0, info.get("height") or 0
                if not row["is_vr"] and not row["vr_locked"] and w >= 3840 and h and abs(w / h - 2.0) < 0.01:
                    fields.update(is_vr=1, vr_layout=LAYOUT_SBS, vr_proj=PROJ_EQUI180)
                db.set_fields(row["id"], probed=1, **fields)
                self.progress.emit("Reading video details", n, len(rows))
                if n % 40 == 0:
                    self.library_changed.emit()

    def _thumb_all(self, db: Database, position: float) -> None:
        rows = [dict(r) for r in db.missing_thumbs()]
        if not rows:
            return
        out_dir = paths.thumbs_dir()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(make_thumb, r, str(out_dir / f"{r['id']}.jpg"), position): r for r in rows}
            for n, future in enumerate(as_completed(futures), 1):
                if self._stop:
                    pool.shutdown(cancel_futures=True)
                    return
                row = futures[future]
                name = f"{row['id']}.jpg" if future.result() else ""
                db.set_fields(row["id"], thumb=name)
                self.thumb_ready.emit(row["id"], name)
                self.progress.emit("Making thumbnails", n, len(rows))


def remove_thumb(video_id: int) -> None:
    try:
        os.remove(paths.thumbs_dir() / f"{video_id}.jpg")
    except OSError:
        pass


def regenerate_thumb(db: Database, video_id: int) -> bool:
    video = db.get_video(video_id)
    if not video:
        return False
    if not video["probed"]:
        info = probe(video["path"]) or {}
        fields = {k: v for k, v in info.items() if v is not None}
        db.set_fields(video_id, probed=1, **fields)
        video.update(fields)
    out = paths.thumbs_dir() / f"{video_id}.jpg"
    ok = make_thumb(video, str(out), db.get_setting("thumb_position", 0.3))
    db.set_fields(video_id, thumb=f"{video_id}.jpg" if ok else "")
    return ok
