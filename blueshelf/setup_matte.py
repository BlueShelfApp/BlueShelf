"""Getting VRAutoMatte onto a machine without anyone opening a terminal.

Everything it needs comes from two downloads and one sync:

  * uv, which brings its own Python with it, so nothing has to be installed first;
  * VRAutoMatte itself as a zip, so git is not needed either.

VRAutoMatte does have one dependency that comes from git - matanyone2 - but it sits in an
optional extra whose build is broken upstream, and a plain sync never asks for it. That is
also why this does not use the project's own run.bat, which passes --all-extras and fails.

The long part is the sync: PyTorch with a CUDA build is several gigabytes, so every step
reports what it is doing and roughly how far along it is, and every step can be stopped.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from . import passthrough, paths

UV_ZIP = ("https://github.com/astral-sh/uv/releases/latest/download/"
          "uv-x86_64-pc-windows-msvc.zip")
UV_SUM = UV_ZIP + ".sha256"
# The default branch is master, not main - main gives a 404 and a baffling error.
MATTE_ZIP = "https://github.com/SifuInTheShell/VRAutoMatte/archive/refs/heads/master.zip"

Report = Callable[[str, float, str], None]
Stopping = Callable[[], bool]


class Stopped(Exception):
    """Raised when the person asked for the setup to stop."""


def _nothing(stage: str, share: float, detail: str) -> None:
    pass


def default_home() -> Path:
    """Where VRAutoMatte goes when nobody says otherwise: beside BlueShelf's own data."""
    return paths.data_dir() / "vrautomatte"


def find_uv(nearby: str | Path | None = None) -> Path | None:
    """uv, wherever this machine happens to keep it.

    Ours first, then the ones the official installers use. A machine that already has uv
    should not be made to download another copy.
    """
    looked: list[Path] = []
    if nearby:
        looked.append(Path(nearby) / "uv.exe")
    looked.append(our_uv())
    on_path = shutil.which("uv")
    if on_path:
        looked.append(Path(on_path))
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA") or str(home)
    looked += [
        home / ".local" / "bin" / "uv.exe",
        Path(local) / "Microsoft" / "WinGet" / "Links" / "uv.exe",
        Path(local) / "uv" / "uv.exe",
    ]
    for candidate in looked:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def our_uv() -> Path:
    """The copy BlueShelf downloads for itself, kept out of the way of any other."""
    return paths.data_dir() / "tools" / ("uv.exe" if os.name == "nt" else "uv")


# --------------------------------------------------------------------------- downloading

def download(url: str, into: str | Path, report: Report = _nothing, stage: str = "Downloading",
             at: float = 0.0, span: float = 1.0, expect: str | None = None,
             should_stop: Stopping | None = None, opener=urlopen) -> Path:
    """Fetch a file, saying how far along it is, and check it if a checksum was given.

    Written to a part file and moved into place at the end, so an interrupted download is
    never mistaken next time for one that finished.
    """
    into = Path(into)
    into.parent.mkdir(parents=True, exist_ok=True)
    part = into.with_name(into.name + ".part")
    digest = hashlib.sha256()
    got = 0
    try:
        with opener(Request(url, headers={"User-Agent": "BlueShelf"})) as answer:
            total = int(answer.headers.get("Content-Length") or 0)
            with open(part, "wb") as keep:
                while True:
                    if should_stop is not None and should_stop():
                        raise Stopped()
                    lump = answer.read(1 << 20)
                    if not lump:
                        break
                    keep.write(lump)
                    digest.update(lump)
                    got += len(lump)
                    share = at + span * (got / total if total else 0.0)
                    report(stage, min(share, at + span),
                           f"{got / 1e6:,.0f} MB of {total / 1e6:,.0f} MB" if total
                           else f"{got / 1e6:,.0f} MB")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if expect and digest.hexdigest().lower() != expect.lower():
        part.unlink(missing_ok=True)
        raise passthrough.Unavailable(
            f"{into.name} did not arrive intact - its checksum does not match what "
            f"the release says it should be. Nothing was installed.")
    os.replace(part, into)
    return into


def _checksum(url: str, opener=urlopen) -> str | None:
    """The hash out of a .sha256 file, which reads "<hash>  <filename>"."""
    try:
        with opener(Request(url, headers={"User-Agent": "BlueShelf"})) as answer:
            said = answer.read(200).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a missing checksum is not a reason to stop
        return None
    first = said.strip().split()
    return first[0] if first and len(first[0]) == 64 else None


def unpack(archive: str | Path, into: str | Path, strip_top: bool = False) -> Path:
    """Extract a zip, optionally dropping the single folder GitHub wraps a branch in."""
    archive, into = Path(archive), Path(into)
    into.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        names = [n for n in bundle.namelist() if not n.endswith("/")]
        tops = {n.split("/", 1)[0] for n in names}
        lead = f"{tops.pop()}/" if strip_top and len(tops) == 1 else ""
        for name in names:
            if not name.startswith(lead):
                continue
            # No absolute paths and nothing climbing out of the folder we chose.
            inner = Path(name[len(lead):])
            if inner.is_absolute() or ".." in inner.parts:
                continue
            target = into / inner
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(name) as source, open(target, "wb") as keep:
                shutil.copyfileobj(source, keep)
    return into


# --------------------------------------------------------------------------- the steps

def install_uv(report: Report = _nothing, at: float = 0.0, span: float = 0.10,
               should_stop: Stopping | None = None, opener=urlopen) -> Path:
    """Put a copy of uv where BlueShelf can find it again."""
    where = our_uv()
    where.parent.mkdir(parents=True, exist_ok=True)
    keep = where.parent / "uv.zip"
    download(UV_ZIP, keep, report, "Fetching uv", at, span * 0.9,
             expect=_checksum(UV_SUM, opener), should_stop=should_stop, opener=opener)
    report("Unpacking uv", at + span * 0.95, "")
    unpack(keep, where.parent)
    keep.unlink(missing_ok=True)
    if not where.is_file():
        raise passthrough.Unavailable("The uv download did not contain uv.exe.")
    return where


def install_matte(home: str | Path, report: Report = _nothing, at: float = 0.10,
                  span: float = 0.10, should_stop: Stopping | None = None,
                  opener=urlopen) -> Path:
    """Put VRAutoMatte's own files in place, as a zip rather than a clone."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    keep = home / "vrautomatte.zip"
    download(MATTE_ZIP, keep, report, "Fetching VRAutoMatte", at, span * 0.8,
             should_stop=should_stop, opener=opener)
    report("Unpacking VRAutoMatte", at + span * 0.9, "")
    unpack(keep, home, strip_top=True)
    keep.unlink(missing_ok=True)
    if not (home / "pyproject.toml").is_file():
        raise passthrough.Unavailable(
            "The VRAutoMatte download did not look like the project - no pyproject.toml.")
    return home


def sync(home: str | Path, uv: str | Path, report: Report = _nothing, at: float = 0.20,
         span: float = 0.80, should_stop: Stopping | None = None,
         runner: Callable[..., subprocess.Popen] | None = None) -> None:
    """Build the environment: uv fetches a Python of its own and then the packages.

    Plain sync, never --all-extras. The extras pull matanyone2 from git, which does not
    build upstream, and none of them are needed for the matting BlueShelf asks for.
    """
    home = Path(home)
    start = runner or (lambda args, **rest: subprocess.Popen(args, **rest))
    running = start(
        [str(uv), "sync", "--python-preference", "managed"],
        cwd=str(home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
        creationflags=paths.NO_WINDOW,
        env={**os.environ, "UV_NO_PROGRESS": "1", "PYTHONUNBUFFERED": "1"})
    seen, last = 0, ""
    for line in running.stdout or []:
        if should_stop is not None and should_stop():
            running.kill()
            raise Stopped()
        line = line.strip()
        if not line:
            continue
        last = line
        seen += 1
        # There is no total to count against, so the bar creeps and the line carries the
        # news. Most of the wait is one download of several gigabytes.
        report("Building the matting environment", at + span * min(0.95, seen / 400), line)
    if running.wait() != 0:
        raise passthrough.Unavailable(
            f"Building the matting environment failed. The last thing it said was:\n{last}")


def set_up(home: str | Path | None = None, report: Report = _nothing,
           should_stop: Stopping | None = None, opener=urlopen,
           runner: Callable[..., subprocess.Popen] | None = None) -> Path:
    """The whole thing, start to finish, skipping whatever is already done."""
    home = Path(home) if home else default_home()
    report("Looking for what is already here", 0.0, "")
    uv = find_uv(home)
    if uv is None:
        uv = install_uv(report, should_stop=should_stop, opener=opener)
    else:
        report("uv is already installed", 0.10, str(uv))

    if not (home / "pyproject.toml").is_file():
        install_matte(home, report, should_stop=should_stop, opener=opener)
    else:
        report("VRAutoMatte's files are already here", 0.20, str(home))

    if passthrough.looks_like_install(home):
        report("Already built", 1.0, str(home))
        return home
    sync(home, uv, report, should_stop=should_stop, runner=runner)
    if not passthrough.looks_like_install(home):
        raise passthrough.Unavailable(
            "The build finished but the folder still does not look like a working "
            "VRAutoMatte - there is no .venv with a Python in it.")
    report("Ready", 1.0, str(home))
    return home
