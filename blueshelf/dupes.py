"""Duplicate detection: byte-identical copies and visually matching re-encodes.

Identical files are found by size plus a hash of three 1 MB samples. Re-encodes
(different resolution, bitrate or container) are found by comparing tiny
difference hashes of frames sampled across each video.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QThread, Signal

from blueshelf import paths
from blueshelf.db import Database

SIGNATURE_VERSION = "v1"
FRAMES = 16
CHUNK = 1 << 20
MAX_BIT_DISTANCE = 10
MIN_MATCH_FRACTION = 0.6
MIN_VALID_FRAMES = 5


def quick_hash(path: str, size: int) -> str | None:
    h = hashlib.sha1(str(size).encode())
    try:
        with open(path, "rb") as f:
            for offset in (0, max(0, size // 2 - CHUNK // 2), max(0, size - CHUNK)):
                f.seek(offset)
                h.update(f.read(CHUNK))
    except OSError:
        return None
    return h.hexdigest()


def _dhash(pixels: bytes) -> int | None:
    if len(pixels) != 72 or max(pixels) - min(pixels) < 12:
        return None  # unreadable, black or flat frame: useless for matching
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (pixels[base + col] > pixels[base + col + 1])
    return bits


def frame_signature(path: str, duration: float) -> str:
    hashes = []
    for i in range(FRAMES):
        t = duration * (0.08 + 0.84 * i / (FRAMES - 1))
        try:
            result = paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-hwaccel", "auto", "-ss", f"{t:.2f}",
                                     "-i", path, "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray",
                                     "-f", "rawvideo", "-"], timeout=120)
            hashes.append(_dhash(result.stdout))
        except Exception:
            hashes.append(None)
    return SIGNATURE_VERSION + ":" + ",".join("-" if h is None else f"{h:016x}" for h in hashes)


def parse_signature(sig: str | None) -> list[int] | None:
    if not sig or not sig.startswith(SIGNATURE_VERSION + ":"):
        return None
    return [int(x, 16) for x in sig.split(":", 1)[1].split(",") if x != "-"]


def similarity(a: list[int], b: list[int]) -> float:
    if len(a) < MIN_VALID_FRAMES or len(b) < MIN_VALID_FRAMES:
        return 0.0

    def one_way(xs, ys):
        return sum(1 for x in xs if min((x ^ y).bit_count() for y in ys) <= MAX_BIT_DISTANCE) / len(xs)

    return min(one_way(a, b), one_way(b, a))


class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


class DupeScanThread(QThread):
    progress = Signal(str, int, int)
    finished_groups = Signal(list)

    def __init__(self, visual: bool = True, parent=None):
        super().__init__(parent)
        self.visual = visual
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        db = Database(str(paths.db_path()))
        try:
            groups = self._find(db)
        finally:
            db.close()
        self.finished_groups.emit(groups)

    def _find(self, db: Database) -> list[dict]:
        rows = {r["id"]: dict(r) for r in db.dupe_candidates()}
        uf = _UnionFind()

        by_size: dict[int, list[dict]] = {}
        for r in rows.values():
            by_size.setdefault(r["size"], []).append(r)
        need_hash = [r for group in by_size.values() if len(group) > 1 for r in group if not r["quick_hash"]]
        self._run_pool(need_hash, lambda r: quick_hash(r["path"], r["size"]), "quick_hash", db,
                       "Comparing file contents", workers=4)
        by_hash: dict[str, list[int]] = {}
        for group in by_size.values():
            if len(group) > 1:
                for r in group:
                    if r["quick_hash"]:
                        by_hash.setdefault(r["quick_hash"], []).append(r["id"])
        for ids in by_hash.values():
            for other in ids[1:]:
                uf.union(ids[0], other)

        if self.visual and not self._stop:
            need_sig = [r for r in rows.values() if r["duration"] and parse_signature(r["phash"]) is None]
            self._run_pool(need_sig, lambda r: frame_signature(r["path"], r["duration"]), "phash", db,
                           "Fingerprinting videos", workers=6)
            sigs = [(r["duration"], r["id"], parse_signature(r["phash"])) for r in rows.values() if r["duration"]]
            sigs = [s for s in sigs if s[2]]
            sigs.sort()
            for i, (dur_a, id_a, sig_a) in enumerate(sigs):
                if self._stop:
                    break
                for dur_b, id_b, sig_b in sigs[i + 1:]:
                    if dur_b > dur_a * 1.15 + 30:
                        break
                    if uf.find(id_a) != uf.find(id_b) and similarity(sig_a, sig_b) >= MIN_MATCH_FRACTION:
                        uf.union(id_a, id_b)
                if i % 50 == 0:
                    self.progress.emit("Matching fingerprints", i, len(sigs))

        clusters: dict[int, list[int]] = {}
        for vid in rows:
            if vid in uf.parent:
                clusters.setdefault(uf.find(vid), []).append(vid)
        groups = []
        for ids in clusters.values():
            if len(ids) < 2:
                continue
            hashes = {rows[i]["quick_hash"] for i in ids}
            kind = "identical" if len(hashes) == 1 and None not in hashes else "similar"
            groups.append({"kind": kind, "ids": sorted(ids, key=lambda i: _quality(rows[i]), reverse=True)})
        groups.sort(key=lambda g: (g["kind"] != "identical", -sum(rows[i]["size"] for i in g["ids"])))
        return groups

    def _run_pool(self, items: list[dict], fn, column: str, db: Database, label: str, workers: int) -> None:
        if not items:
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fn, r): r for r in items}
            for n, future in enumerate(as_completed(futures), 1):
                if self._stop:
                    pool.shutdown(cancel_futures=True)
                    return
                row = futures[future]
                row[column] = future.result()
                db.set_fields(row["id"], **{column: row[column]})
                self.progress.emit(label, n, len(items))


def _quality(r: dict) -> tuple:
    """Sort key for picking which copy to keep: resolution, then bitrate, then size."""
    return ((r["width"] or 0) * (r["height"] or 0), r["bitrate"] or 0, r["size"])
