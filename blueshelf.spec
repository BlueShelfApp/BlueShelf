# PyInstaller build description. Run build.ps1 rather than calling this directly.
from pathlib import Path

root = Path(SPECPATH)
bundled = [(str(p), "bin") for p in (root / "bin").iterdir() if p.suffix.lower() in (".dll", ".exe")]

a = Analysis(
    [str(root / "run_blueshelf.py")],
    pathex=[str(root)],
    binaries=bundled,
    datas=[(str(root / "blueshelf" / "shaders"), "blueshelf/shaders"),
           (str(root / "blueshelf" / "web"), "blueshelf/web"),
           # Read and run by VRAutoMatte's interpreter, not this one, so it has to survive
           # as a real .py file rather than being frozen into the bundle.
           (str(root / "blueshelf" / "matte_worker.py"), "blueshelf")],
    excludes=["tkinter", "pytest"],
)
# PyInstaller also collects the DLLs that ffmpeg.exe depends on into the root of _internal, which
# duplicates the copies in bin/ (about 150 MB). The app only ever loads them from bin/.
bundled_names = {Path(dest).name.lower() for dest, _src, _kind in a.binaries if Path(dest).parent.name == "bin"}
a.binaries = [entry for entry in a.binaries
              if not (str(Path(entry[0]).parent) == "." and Path(entry[0]).name.lower() in bundled_names)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BlueShelf",
    icon=str(root / "build" / "blueshelf.ico"),
    version=str(root / "build" / "version.txt"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="BlueShelf", upx=False)
