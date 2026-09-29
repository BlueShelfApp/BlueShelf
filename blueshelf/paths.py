"""Locations of bundled tools, packaged resources and per-user data."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "BlueShelf"
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    PACKAGE_DIR = RESOURCE_ROOT / "blueshelf"
else:
    PACKAGE_DIR = Path(__file__).resolve().parent
    RESOURCE_ROOT = PACKAGE_DIR.parent

BIN_DIR = RESOURCE_ROOT / "bin"
SHADER_PATH = PACKAGE_DIR / "shaders" / "vr.glsl"

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


LEGACY_APP_NAME = "Vault"  # what this app was called before it was BlueShelf
_migrated = False


def _bring_forward_old_data(path: Path) -> None:
    """Move a library left behind by the old name, so nobody starts again from nothing."""
    global _migrated
    _migrated = True
    local = os.environ.get("LOCALAPPDATA")
    if not local or path.exists():
        return
    old = Path(local) / LEGACY_APP_NAME
    if not (old / "library.db").is_file():
        return
    try:
        old.rename(path)
    except OSError:
        try:
            shutil.copytree(old, path)  # in use, or across drives: copy and leave the old one
        except OSError:
            pass


def data_dir() -> Path:
    base = os.environ.get("BLUESHELF_DATA_DIR")
    if not base:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()), APP_NAME)
    path = Path(base)
    if not _migrated and not os.environ.get("BLUESHELF_DATA_DIR"):
        _bring_forward_old_data(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def thumbs_dir() -> Path:
    path = data_dir() / "thumbs"
    path.mkdir(exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "library.db"


def tool(name: str) -> str:
    bundled = BIN_DIR / f"{name}.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which(name) or name


def prepare_mpv_dll() -> None:
    """Make the bundled libmpv visible to python-mpv; call before importing mpv."""
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory") and BIN_DIR.exists():
        os.add_dll_directory(str(BIN_DIR))


def run_tool(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
