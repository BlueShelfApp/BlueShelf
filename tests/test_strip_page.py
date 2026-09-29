"""The scan bar's stills, end to end: made on the PC, fetched by the page, shown when pointing."""
from __future__ import annotations

import time

from blueshelf import paths
from test_page import open_the_video, page  # noqa: F401


def test_the_stills_arrive_and_are_shown_when_pointing(page, tmp_path, monkeypatch):
    browser, server = page
    from blueshelf import media
    monkeypatch.setattr(media, "STRIPS", media.StripMaker())
    clip = tmp_path / "library" / "TestVR.Anna.Bell.180_LR.mp4"
    # A key frame a second, as real films have - the stills are taken from key frames.
    paths.run_tool([paths.tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=1280x640:rate=10:duration=6", "-c:v", "libopenh264", "-g", "10",
                    "-pix_fmt", "yuv420p", str(clip)], timeout=120)
    open_the_video(browser, server)
    deadline = time.time() + 120
    arrived = None
    while time.time() < deadline:
        arrived = browser.ask("""
          (() => { const s = window.blueshelfDebug.shelf.strip;
                   return s ? { loaded: s.image.complete && s.image.naturalWidth, url: s.url } : null; })()
        """)
        if arrived and arrived["loaded"]:
            break
        time.sleep(1)
    assert arrived and arrived["loaded"] == 1600, arrived
    shown = browser.ask("""
      (async () => {
        const track = document.getElementById('sh-track').getBoundingClientRect();
        document.getElementById('sh-scrub').dispatchEvent(new PointerEvent('pointermove',
          { clientX: track.left + track.width * 0.55, clientY: track.top + 5, bubbles: true }));
        await new Promise((r) => setTimeout(r, 300));
        const thumb = document.getElementById('sh-thumb');
        const style = getComputedStyle(thumb);
        // And the headset's bubble, drawn from the same stills.
        const d = window.blueshelfDebug;
        d.buildPanel(); d.vr.panel.visible = true; d.drawShelfVR(true);
        const ctx = d.vr.ui.bubble.surface.canvas.getContext('2d');
        const s = d.vr.ui.bubble.surface.scale, m = d.vr.ui.bubble.surface.margin;
        const px = ctx.getImageData(Math.round((m + 100) * s), Math.round((m + 60) * s), 1, 1).data;
        return { image: style.backgroundImage, size: style.backgroundSize, at: style.backgroundPosition,
                 bubble: !document.getElementById('sh-bubble').hidden, vrPixel: [px[0], px[1], px[2]] };
      })()
    """)
    assert shown["bubble"] and "strip/" in shown["image"], shown
    assert shown["size"] == "2200px 1220px", shown
    grey = (0x47, 0x42, 0x38)
    assert tuple(shown["vrPixel"]) != grey, f"the headset bubble shows a still, not the grey box: {shown['vrPixel']}"
