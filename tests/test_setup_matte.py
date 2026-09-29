"""Setting VRAutoMatte up from inside BlueShelf, without a terminal.

Nothing here touches the network or starts a process: the downloader and the runner are
both handed in, so what is checked is the decisions, not GitHub's uptime.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

import pytest

from blueshelf import passthrough, setup_matte


class _Answer:
    """What urlopen hands back: a reader with headers, usable as a context manager."""

    def __init__(self, body: bytes, length: bool = True):
        self._body = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))} if length else {}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _serving(files: dict[str, bytes]):
    def opener(request):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for ending, body in files.items():
            if url.endswith(ending):
                return _Answer(body)
        raise AssertionError(f"nothing is serving {url}")
    return opener


def _zip(entries: dict[str, bytes]) -> bytes:
    keep = io.BytesIO()
    with zipfile.ZipFile(keep, "w") as bundle:
        for name, body in entries.items():
            bundle.writestr(name, body)
    return keep.getvalue()


# --------------------------------------------------------------------------- unpacking

def test_the_folder_github_wraps_a_branch_in_is_dropped(tmp_path):
    """A branch zip holds everything under VRAutoMatte-master/, which nobody wants."""
    archive = tmp_path / "branch.zip"
    archive.write_bytes(_zip({"VRAutoMatte-master/pyproject.toml": b"[project]",
                              "VRAutoMatte-master/src/vrautomatte/main.py": b"pass"}))

    setup_matte.unpack(archive, tmp_path / "out", strip_top=True)

    assert (tmp_path / "out" / "pyproject.toml").read_bytes() == b"[project]"
    assert (tmp_path / "out" / "src" / "vrautomatte" / "main.py").is_file()
    assert not (tmp_path / "out" / "VRAutoMatte-master").exists()


def test_an_entry_climbing_out_of_the_folder_is_ignored(tmp_path):
    """A zip is a download from the internet, so it does not get to choose where it lands."""
    archive = tmp_path / "nasty.zip"
    archive.write_bytes(_zip({"fine.txt": b"ok", "../escaped.txt": b"no"}))

    setup_matte.unpack(archive, tmp_path / "out")

    assert (tmp_path / "out" / "fine.txt").is_file()
    assert not (tmp_path / "escaped.txt").exists()


# --------------------------------------------------------------------------- downloading

def test_a_download_that_does_not_match_its_checksum_is_thrown_away(tmp_path):
    opener = _serving({"thing.zip": b"not what was promised"})

    with pytest.raises(passthrough.Unavailable, match="did not arrive intact"):
        setup_matte.download("https://x/thing.zip", tmp_path / "thing.zip",
                             expect="0" * 64, opener=opener)

    assert not (tmp_path / "thing.zip").exists(), "nothing half-installed is left behind"
    assert not (tmp_path / "thing.zip.part").exists()


def test_a_good_download_is_kept_and_reported(tmp_path):
    body = b"x" * 3_000_000
    seen: list[tuple[str, float, str]] = []
    setup_matte.download("https://x/thing.zip", tmp_path / "thing.zip",
                         lambda s, f, d: seen.append((s, f, d)),
                         expect=hashlib.sha256(body).hexdigest(),
                         opener=_serving({"thing.zip": body}))

    assert (tmp_path / "thing.zip").read_bytes() == body
    assert seen and seen[-1][1] <= 1.0
    assert all(a[1] <= b[1] for a, b in zip(seen, seen[1:])), "the bar never goes backwards"


def test_stopping_a_download_leaves_nothing_behind(tmp_path):
    with pytest.raises(setup_matte.Stopped):
        setup_matte.download("https://x/thing.zip", tmp_path / "thing.zip",
                             should_stop=lambda: True,
                             opener=_serving({"thing.zip": b"y" * 3_000_000}))
    assert not (tmp_path / "thing.zip").exists()
    assert not (tmp_path / "thing.zip.part").exists(), "no half a download left lying about"


# --------------------------------------------------------------------------- finding uv

def test_our_own_uv_is_preferred_to_hunting_the_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(tmp_path / "data"))
    mine = setup_matte.our_uv()
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"uv")

    assert setup_matte.find_uv() == mine


def test_a_uv_beside_the_install_is_found_first(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(tmp_path / "data"))
    beside = tmp_path / "home" / "uv.exe"
    beside.parent.mkdir(parents=True)
    beside.write_bytes(b"uv")

    assert setup_matte.find_uv(tmp_path / "home") == beside


# --------------------------------------------------------------------------- the sync

def _fake_run(lines: list[str], code: int = 0):
    seen: dict = {}

    class _Running:
        def __init__(self):
            self.stdout = iter(f"{line}\n" for line in lines)
            self.killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return code

    def start(args, **rest):
        seen["args"] = args
        seen["cwd"] = rest.get("cwd")
        seen["running"] = _Running()
        return seen["running"]

    return start, seen


def test_the_sync_never_asks_for_the_extras(tmp_path):
    """--all-extras pulls matanyone2 from git, which does not build upstream."""
    start, seen = _fake_run(["Resolved 40 packages", "Installed 40 packages"])

    setup_matte.sync(tmp_path, "uv.exe", runner=start)

    assert seen["args"][1] == "sync"
    assert "--all-extras" not in seen["args"]
    assert seen["cwd"] == str(tmp_path)


def test_a_failed_sync_says_what_it_last_said(tmp_path):
    start, _seen = _fake_run(["Resolved 40 packages", "error: no such index"], code = 1)

    with pytest.raises(passthrough.Unavailable, match="no such index"):
        setup_matte.sync(tmp_path, "uv.exe", runner=start)


def test_stopping_the_sync_kills_it(tmp_path):
    start, seen = _fake_run(["Resolved 40 packages", "Downloading torch"])

    with pytest.raises(setup_matte.Stopped):
        setup_matte.sync(tmp_path, "uv.exe", runner=start, should_stop=lambda: True)

    assert seen["running"].killed, "a sync left running would keep downloading gigabytes"


# --------------------------------------------------------------------------- all together

def _a_working_install(home: Path) -> None:
    (home / "src" / "vrautomatte").mkdir(parents=True, exist_ok=True)
    (home / "src" / "vrautomatte" / "main.py").write_text("pass", encoding="utf-8")
    python = passthrough.python_of(home)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"python")


def test_setting_up_does_the_whole_thing(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(tmp_path / "data"))
    home = tmp_path / "home"
    uv_zip = _zip({"uv.exe": b"uv"})
    branch = _zip({"VRAutoMatte-master/pyproject.toml": b"[project]"})
    opener = _serving({"uv-x86_64-pc-windows-msvc.zip.sha256":
                       f"{hashlib.sha256(uv_zip).hexdigest()}  uv.zip".encode(),
                       "uv-x86_64-pc-windows-msvc.zip": uv_zip,
                       "master.zip": branch})

    def start(args, **rest):
        _a_working_install(home)                 # what a real sync leaves behind
        return _fake_run(["Installed 40 packages"])[0](args, **rest)

    where = setup_matte.set_up(home, opener=opener, runner=start)

    assert where == home
    assert passthrough.looks_like_install(home)
    assert setup_matte.our_uv().is_file(), "uv kept for next time"
    assert not (home / "vrautomatte.zip").exists(), "the download is tidied away"


def test_nothing_is_downloaded_twice(tmp_path, monkeypatch):
    """Pressing the button on a machine that is already set up must not re-download 5 GB."""
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(tmp_path / "data"))
    home = tmp_path / "home"
    home.mkdir()
    (home / "pyproject.toml").write_text("[project]", encoding="utf-8")
    _a_working_install(home)
    mine = setup_matte.our_uv()
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"uv")

    def refuse(_request):
        raise AssertionError("nothing should be downloaded")

    def never(*_a, **_k):
        raise AssertionError("nothing should be built")

    steps: list[str] = []
    where = setup_matte.set_up(home, lambda s, f, d: steps.append(s),
                               opener=refuse, runner=never)

    assert where == home
    assert "Already built" in steps


def test_a_broken_download_does_not_look_like_an_install(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(tmp_path / "data"))
    home = tmp_path / "home"
    mine = setup_matte.our_uv()
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"uv")
    opener = _serving({"master.zip": _zip({"VRAutoMatte-master/README.md": b"hi"})})

    with pytest.raises(passthrough.Unavailable, match="no pyproject.toml"):
        setup_matte.set_up(home, opener=opener, runner=lambda *a, **k: None)
