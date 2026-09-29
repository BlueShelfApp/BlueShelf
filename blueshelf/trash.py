"""Deleting videos: a hidden trash folder per library, restore, and permanent removal."""
from __future__ import annotations

import os
import shutil
import stat
import time

from blueshelf.parser import VIDEO_EXTS, is_person_folder

TRASH_DIR_NAME = ".blueshelf-trash"
LEFTOVER_LIMIT = 64 * 1024 * 1024


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def removal_target(video_path: str, library_root: str) -> str:
    """What to remove for this video: its release folder when that folder holds nothing else worth keeping."""
    parent = os.path.dirname(video_path)
    root = _norm(library_root)
    if _norm(parent) == root or not _norm(parent).startswith(root + os.sep):
        return video_path
    if is_person_folder(os.path.basename(parent)):
        return video_path
    leftovers = 0
    for dirpath, _, files in os.walk(parent):
        for name in files:
            full = os.path.join(dirpath, name)
            if _norm(full) == _norm(video_path):
                continue
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS and "sample" not in name.lower():
                return video_path
            try:
                leftovers += os.path.getsize(full)
            except OSError:
                return video_path
            if leftovers > LEFTOVER_LIMIT:
                return video_path
    return parent


def _hide(path: str) -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass


def move_to_trash(video_path: str, library_root: str) -> tuple[str, str]:
    """Move the video (or its release folder) into the library's trash. Returns (trash item, original path)."""
    target = removal_target(video_path, library_root)
    trash_root = os.path.join(library_root, TRASH_DIR_NAME)
    if not os.path.isdir(trash_root):
        os.makedirs(trash_root, exist_ok=True)
        _hide(trash_root)
    base = f"{time.strftime('%Y%m%d-%H%M%S')} {os.path.basename(target)}"
    dest = os.path.join(trash_root, base)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(trash_root, f"{base} ({n})")
        n += 1
    os.rename(target, dest)
    return dest, target


def restore(item: str, origin: str) -> None:
    if os.path.exists(origin):
        raise FileExistsError(f"Something already exists at {origin}")
    os.makedirs(os.path.dirname(origin), exist_ok=True)
    os.rename(item, origin)


def _make_writable_and_retry(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_path(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_make_writable_and_retry)
    elif os.path.lexists(path):
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)


def delete_permanently(video_path: str, library_root: str) -> str:
    target = removal_target(video_path, library_root)
    remove_path(target)
    return target


def trash_contents(library_root: str) -> list[str]:
    root = os.path.join(library_root, TRASH_DIR_NAME)
    try:
        return [os.path.join(root, name) for name in sorted(os.listdir(root))]
    except OSError:
        return []


def path_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for dirpath, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total
