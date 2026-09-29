# Third-party components

BlueShelf is distributed with the components below. Each keeps its own licence. Every one of them is
LGPL, MIT or PSF - none is GPL - so they place no licence of their own on BlueShelf's code. BlueShelf's
own licence is in `LICENSE` and is a separate choice.

| Component | Version | Licence | Used for |
| --- | --- | --- | --- |
| [mpv](https://mpv.io) (libmpv) | 0.41.0-1023-g69e63f425, LGPL build | LGPL v2.1 or later | Video playback, hardware decoding, the VR view |
| [FFmpeg](https://ffmpeg.org) | n8.1.2, LGPL build (`--enable-version3`, no `--enable-gpl`) | LGPL v3 | Reading video details, thumbnails, duplicate fingerprints |
| [Qt](https://www.qt.io) via [PySide6](https://doc.qt.io/qtforpython/) | 6.x | LGPL v3 | The desktop interface |
| [three.js](https://threejs.org) | r160 | MIT | The VR view in the headset browser |
| [PyInstaller](https://pyinstaller.org) | 6.x | GPL v2+ with a bootloader exception | Building the installer |
| [Python](https://www.python.org) | 3.10 | PSF License | The language and standard library |

## What this means if you pass BlueShelf on

Every bundled component is under the **LGPL** (mpv, FFmpeg, Qt) or a permissive licence (three.js,
Python). The LGPL asks three things of whoever ships them, and BlueShelf already meets all three:

1. **Keep them as separate, replaceable libraries.** They are ordinary DLLs in the installed folder -
   `_internalin` for FFmpeg and libmpv, `_internal` for Qt - and BlueShelf loads whatever is there, so
   anyone can swap in their own build.
2. **Say what is in here and under what licence.** This file, kept with any copy you pass on.
3. **Make the source of those libraries available** to anyone you give a build to. Links to the
   projects are enough while they publish it themselves; keeping a copy of the exact builds you
   shipped is the safer habit.

The LGPL asks nothing about BlueShelf's own code. PyInstaller's licence carries an explicit exception
for the programs it packages, so it asks nothing either. **Whether BlueShelf's source is published is
therefore your decision, not an obligation** - see `LICENSE` for the choice currently made.

If you ever replace libmpv with a standard (non-LGPL) mpv build, or FFmpeg with a `--enable-gpl`
build, that stops being true: those are GPL, and a GPL library does place its licence on the whole
program. Check `ffmpeg -version` for `--enable-gpl`, and the libmpv build's own notice, before
swapping either one.

## Licence texts

- GPL v3: `LICENSE` in this folder, also at <https://www.gnu.org/licenses/gpl-3.0.txt>
- LGPL v2.1: <https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt>
- LGPL v3: <https://www.gnu.org/licenses/lgpl-3.0.txt>
- MIT (three.js): <https://github.com/mrdoob/three.js/blob/dev/LICENSE>
- Python: <https://docs.python.org/3/license.html>

## Replacing the bundled libraries

Qt and FFmpeg are included under the LGPL, which requires that you be able to replace them with your
own versions. They are ordinary DLLs in the installed folder — `_internal\bin` for FFmpeg and libmpv,
`_internal` for Qt — and BlueShelf loads whatever is there, so swapping in a compatible build is enough.
