"""Not a test: pictures of the Shelf controls, to compare against the design by eye.

    .venv\\Scripts\\python.exe -m pytest tests\\shelf_look.py -s --out <folder>
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from test_page import open_the_video, page  # noqa: F401


def save(browser, into: Path, name: str) -> None:
    shot = browser.send("Page.captureScreenshot", format="png")
    (into / f"{name}.png").write_bytes(base64.b64decode(shot["data"]))


def test_pictures(page, request):
    browser, server = page
    into = Path(os.environ.get("SHELF_LOOK", "."))
    into.mkdir(parents=True, exist_ok=True)
    browser.send("Emulation.setDeviceMetricsOverride", width=1200, height=760, deviceScaleFactor=1, mobile=False)
    open_the_video(browser, server)
    browser.ask("window.blueshelfDebug.video.pause(), window.blueshelfDebug.video.currentTime = 2.5, true")
    time.sleep(1)
    browser.ask("window.blueshelfDebug.video.play(), true")
    time.sleep(0.6)
    browser.ask("""(() => { const d = window.blueshelfDebug;
        d.shelf.scenes = [{ id: 1, label: 'Scene 1', start: 0.5, end: 2.2 }, { id: 2, label: 'The pool', start: 3.4, end: 5.2 }];
        d.shelf.marking = 2.6; d.act('noop'); return true; })()""")
    time.sleep(0.4)
    save(browser, into, "page-default")
    browser.ask("""(() => { const d = window.blueshelfDebug; d.shelf.scrubHover = 0.62; d.act('noop'); return true; })()""")
    browser.ask("""(() => { const d = window.blueshelfDebug; d.shelf.scrubHover = 0.62; return true; })()""")
    time.sleep(0.3)
    browser.ask("window.blueshelfDebug.act('recenter'), window.blueshelfDebug.shelf.scrubHover = 0.62, true")
    time.sleep(0.3)
    save(browser, into, "page-scrubbing")
    browser.ask("window.blueshelfDebug.shelf.scrubHover = null, window.blueshelfDebug.act('tab:projection'), window.blueshelfDebug.act('settings'), true")
    time.sleep(0.4)
    save(browser, into, "page-settings-projection")
    for tab in ("screen", "audio", "env", "scenes"):
        browser.ask(f"window.blueshelfDebug.act('tab:{tab}'), true")
        time.sleep(0.4)
        save(browser, into, f"page-settings-{tab}")
    browser.ask("window.blueshelfDebug.act('close'), window.blueshelfDebug.video.pause(), true")
    time.sleep(0.4)
    save(browser, into, "page-paused")
    browser.ask("window.blueshelfDebug.act('collapse'), true")
    time.sleep(0.4)
    save(browser, into, "page-collapsed")
    browser.ask("window.blueshelfDebug.act('expand'), true")

    # The headset panel's canvases, as images.
    pictures = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.buildPanel();
        d.vr.panel.visible = true;
        d.shelf.settings = true; d.shelf.tab = 'projection';
        d.shelf.scrubHover = 0.62; d.shelf.hover = 'fwd10';
        d.drawShelfVR(true);
        const out = {};
        for (const name of ['bar', 'settings', 'bubble']) out[name] = d.vr.ui[name].surface.canvas.toDataURL('image/png');
        d.shelf.tab = 'env'; d.drawShelfVR(true);
        out.settingsEnv = d.vr.ui.settings.surface.canvas.toDataURL('image/png');
        d.shelf.tab = 'screen'; d.drawShelfVR(true);
        out.settingsScreen = d.vr.ui.settings.surface.canvas.toDataURL('image/png');
        d.shelf.tab = 'scenes'; d.drawShelfVR(true);
        out.settingsScenes = d.vr.ui.settings.surface.canvas.toDataURL('image/png');
        return out;
      })()
    """)
    for name, url in pictures.items():
        (into / f"vr-{name}.png").write_bytes(base64.b64decode(url.split(",", 1)[1]))
