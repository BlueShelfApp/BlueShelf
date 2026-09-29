"""Plans and builds a browsable folder tree of shortcuts to the videos in the library.

Nothing here ever touches a video file. A scene with two performers gets a shortcut in both
folders, which is the whole reason for using shortcuts instead of moving files: a file can only
live in one folder, but it can be pointed at from many.

The plan is also the groundwork for moving files later. A move needs exactly one destination per
video, which is what primary_folder() decides.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

NO_PERFORMER = "(No performer)"
NO_STUDIO = "(No studio)"
MANIFEST_NAME = ".blueshelf-shortcuts.json"

STRUCTURES = {
    "performer": "A folder per performer",
    "studio_performer": "Studio folders, performers inside",
    "vr_performer": "VR and Flat, performers inside",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
MAX_NAME = 80


def safe_name(name: str, fallback: str = "Unnamed") -> str:
    """A folder or file name Windows will accept."""
    cleaned = " ".join(_ILLEGAL.sub(" ", name).split()).strip(" .")
    if len(cleaned) > MAX_NAME:
        cleaned = cleaned[:MAX_NAME].rstrip(" .")
    if not cleaned or cleaned.lower().split(".")[0] in _RESERVED:
        return fallback
    return cleaned


@dataclass
class ShortcutPlan:
    links: list[tuple[str, str]] = field(default_factory=list)  # (link path relative to the root, video path)
    folders: list[str] = field(default_factory=list)
    unsorted: int = 0  # videos that landed in the "(No performer)" folder

    @property
    def count(self) -> int:
        return len(self.links)


def _folders_for(video: dict, structure: str) -> list[str]:
    performers = [safe_name(p) for p in (video.get("performers") or []) if p.strip()]
    if not performers:
        performers = [NO_PERFORMER]
    if structure == "studio_performer":
        studio = safe_name(video.get("studio") or "", NO_STUDIO)
        return [os.path.join(studio, p) for p in performers]
    if structure == "vr_performer":
        top = "VR" if video.get("is_vr") else "Flat"
        return [os.path.join(top, p) for p in performers]
    return performers


def plan_tree(videos: list[dict], structure: str = "performer") -> ShortcutPlan:
    """Work out where every video's shortcuts go. Videos are dicts as returned by the database."""
    plan = ShortcutPlan()
    folders: dict[str, set[str]] = {}
    for video in videos:
        title = video.get("title") or os.path.splitext(os.path.basename(video["path"]))[0]
        base = safe_name(title, "Video")
        targets = _folders_for(video, structure)
        if any(os.path.basename(t) == NO_PERFORMER for t in targets):
            plan.unsorted += 1
        for folder in targets:
            used = folders.setdefault(folder, set())
            name, n = base, 2
            while name.lower() in used:  # two scenes can share a title inside one folder
                name = f"{base} ({n})"
                n += 1
            used.add(name.lower())
            plan.links.append((os.path.join(folder, name + ".lnk"), video["path"]))
    plan.folders = sorted(folders)
    return plan


def primary_folder(video: dict, popularity: dict[str, int] | None = None, structure: str = "performer") -> str:
    """The single folder a video would move into, for the file-moving version of this.

    With several performers the most-filmed one wins, so a performer's folder stays complete.
    """
    folders = _folders_for(video, structure)
    if len(folders) == 1 or not popularity:
        return folders[0]
    return max(folders, key=lambda folder: (popularity.get(os.path.basename(folder), 0), folder))


# ---------------------------------------------------------------- writing the tree

def unc_path(path: str) -> str:
    """Turn 'Y:\\clip.mp4' into '\\\\server\\share\\clip.mp4' when Y: is a mapped network drive.

    Shortcuts outlive drive letters: the mapping can be missing after a reboot, or on another PC,
    and a shortcut pointing at Y: would then be dead. Local drives are left alone.
    """
    if os.name != "nt" or len(path) < 2 or path[1] != ":":
        return path
    drive = path[:2].upper()
    if drive not in _unc_cache:
        _unc_cache[drive] = ""
        try:
            import ctypes
            from ctypes import wintypes
            buffer = ctypes.create_unicode_buffer(2048)
            size = wintypes.DWORD(len(buffer))
            if ctypes.windll.mpr.WNetGetConnectionW(drive, buffer, ctypes.byref(size)) == 0:
                _unc_cache[drive] = buffer.value.rstrip("\\")
        except Exception:
            pass
    share = _unc_cache[drive]
    return share + path[2:] if share else path


_unc_cache: dict[str, str] = {}


def read_manifest(root: str) -> dict:
    try:
        with open(os.path.join(root, MANIFEST_NAME), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(root: str, plan: ShortcutPlan, structure: str) -> None:
    data = {"created": time.time(), "structure": structure,
            "links": [link for link, _ in plan.links], "folders": plan.folders}
    with open(os.path.join(root, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def check_root(root: str, video_exts: set[str]) -> str | None:
    """Explain why this folder is a bad place for the tree, or None when it is fine."""
    if not root:
        return "Choose a folder for the shortcuts."
    if os.path.isdir(root) and not read_manifest(root):
        for entry in os.scandir(root):
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in video_exts:
                return (f"{root} holds video files. Pick an empty folder, so rebuilding the tree can never "
                        "remove anything of yours.")
            if entry.is_dir() and entry.name.lower() == ".blueshelf-trash":
                return f"{root} is a library folder. Pick a separate folder for the shortcuts."
    return None


def clean_tree(root: str) -> int:
    """Delete only the shortcuts and folders written last time, listed in the manifest."""
    manifest = read_manifest(root)
    removed = 0
    for link in manifest.get("links", []):
        path = os.path.join(root, link)
        if path.lower().endswith(".lnk") and os.path.isfile(path):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    for folder in sorted(manifest.get("folders", []), key=len, reverse=True):
        path = os.path.join(root, folder)
        while os.path.isdir(path) and os.path.normcase(path) != os.path.normcase(root):
            try:
                os.rmdir(path)  # only succeeds while empty, so anything you added is kept
            except OSError:
                break
            path = os.path.dirname(path)
    return removed


_POWERSHELL_SCRIPT = """param([string]$List)
$shell = New-Object -ComObject WScript.Shell
$made = 0
foreach ($line in [System.IO.File]::ReadAllLines($List, [System.Text.Encoding]::UTF8)) {
    if (-not $line) { continue }
    $parts = $line -split "`t"
    try {
        $link = $shell.CreateShortcut($parts[0])
        $link.TargetPath = $parts[1]
        $link.WorkingDirectory = Split-Path -Parent $parts[1]
        $link.Save()
        $made++
    } catch {
        Write-Output ("FAILED`t" + $parts[0] + "`t" + $_.Exception.Message)
    }
}
Write-Output ("MADE`t" + $made)
"""


def write_tree(plan: ShortcutPlan, root: str, structure: str, work_dir: str) -> tuple[int, list[str]]:
    """Create the shortcut files. Returns (how many were made, error lines)."""
    from blueshelf import paths

    os.makedirs(root, exist_ok=True)
    for folder in plan.folders:
        os.makedirs(os.path.join(root, folder), exist_ok=True)
    list_path = os.path.join(work_dir, "blueshelf-shortcuts.txt")
    script_path = os.path.join(work_dir, "blueshelf-shortcuts.ps1")
    with open(list_path, "w", encoding="utf-8") as f:
        for link, target in plan.links:
            f.write(f"{os.path.join(root, link)}\t{unc_path(target)}\n")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(_POWERSHELL_SCRIPT)
    result = paths.run_tool(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path,
                             "-List", list_path], timeout=900)
    made, errors = 0, []
    for line in (result.stdout or b"").decode("utf-8", "replace").splitlines():
        if line.startswith("MADE\t"):
            made = int(line.split("\t")[1])
        elif line.startswith("FAILED\t"):
            errors.append(line.split("\t", 1)[1])
    if made:
        write_manifest(root, plan, structure)
    return made, errors
