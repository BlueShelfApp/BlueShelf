"""The Shelf player controls: drawn as HTML on the page and onto canvases inside the headset,
from one description, and doing the same thing wherever they are pressed."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request

from blueshelf import paths
from test_page import _mask_beside_the_clip, open_the_video, page  # noqa: F401 - fixtures by name


def settle(ms=150):
    time.sleep(ms / 1000)


def detail(server, video_id=1):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/word/api/video/{video_id}", timeout=10) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------- nothing breaks

def test_nothing_throws_while_the_controls_are_used(page):
    browser, server = page
    browser.send("Page.addScriptToEvaluateOnNewDocument", source="""
      window.__errors = [];
      window.addEventListener('error', (e) => window.__errors.push(String(e.message)));
      window.addEventListener('unhandledrejection', (e) => window.__errors.push(String(e.reason)));
    """)
    open_the_video(browser, server)
    errors = browser.ask("""
      (async () => {
        const d = window.blueshelfDebug;
        const wait = (ms) => new Promise((r) => setTimeout(r, ms));
        for (const tab of ['projection', 'screen', 'audio', 'env']) { d.act('tab:' + tab); d.shelf.settings = true; d.act('noop'); await wait(60); }
        d.act('collapse'); await wait(60); d.act('expand'); await wait(60);
        d.buildPanel();
        d.vr.panel.visible = true;
        d.vr.mode = 'immersive';
        for (const tab of ['projection', 'screen', 'audio', 'env']) { d.shelf.tab = tab; d.shelf.settings = true; d.drawShelfVR(true); }
        d.shelf.scrubHover = 0.4; d.drawShelfVR(true);
        d.shelf.collapsed = true; d.drawShelfVR(true); d.shelf.collapsed = false;
        for (const world of ['void', 'cinema', 'lakeside', 'observatory', 'rooftop']) { d.shelf.env = world; d.applyWorld(); }
        d.vr.mode = 'flat'; d.applyWorld();
        return window.__errors;
      })()
    """)
    assert errors == [], errors


# --------------------------------------------------------------------------- the page

def test_the_bar_says_what_is_playing_and_how_it_is_shaped(page):
    browser, server = page
    open_the_video(browser, server)
    seen = browser.ask("""
      (() => ({
        title: document.getElementById('sh-title').textContent,
        lens: document.getElementById('sh-lens').textContent,
        stereo: document.getElementById('sh-stereo').textContent,
        time: document.getElementById('sh-time').textContent,
        playing: !window.blueshelfDebug.video.paused,
        playIcon: document.getElementById('sh-play').innerHTML,
      }))()
    """)
    assert seen["title"] and seen["lens"] == "180°" and seen["stereo"] == "3D Side by side"
    assert seen["time"].endswith("/ 0:06"), seen["time"]
    assert seen["playing"] and "M15 4" in seen["playIcon"], "a playing film shows the pause bars"
    browser.ask("document.getElementById('sh-play').click(), true")
    settle(300)
    assert browser.ask("window.blueshelfDebug.video.paused")
    assert not browser.ask("document.getElementById('shelf-paused').hidden"), "paused says so, as designed"


def test_pointing_at_the_scan_bar_shows_the_time_there_and_its_still(page):
    browser, server = page
    open_the_video(browser, server)
    point = """
      (() => {
        const track = document.getElementById('sh-track').getBoundingClientRect();
        const scrub = document.getElementById('sh-scrub');
        scrub.dispatchEvent(new PointerEvent('pointermove', { clientX: track.left + track.width / 2,
                                                             clientY: track.top + 5, bubbles: true }));
        return true;
      })()
    """
    browser.ask(point)
    settle(200)
    seen = browser.ask("""
      (() => ({ bubble: !document.getElementById('sh-bubble').hidden,
                time: document.getElementById('sh-htime').textContent }))()
    """)
    assert seen["bubble"] and seen["time"] == "0:03", seen

    # With the film's stills made, the right one of the hundred is shown.
    browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.shelf.strip = { image: new Image(), url: 'strip.jpg', count: 100, cols: 10, tileW: 160, tileH: 90 };
        return true;
      })()
    """)
    browser.ask(point)
    settle(200)
    shown = browser.ask("""
      (() => { const s = document.getElementById('sh-thumb').style;
               return { image: s.backgroundImage, at: s.backgroundPosition }; })()
    """)
    assert "strip.jpg" in shown["image"]
    assert shown["at"] in ("-0px -610px", "0px -610px"), f"still 50 is column 0, row 5: {shown['at']}"


def test_the_lens_and_stereo_can_be_corrected_and_are_kept(page):
    """The shape is guessed from the file's name, and the guess is sometimes wrong. It is fixed
    from the settings, and kept, or it is guessed wrong again next time."""
    browser, server = page
    open_the_video(browser, server)
    browser.ask("document.getElementById('sh-lens').click(), true")
    settle(250)
    assert browser.ask("document.querySelector('#shelf-settings .st-label').textContent") == "Projection"
    browser.ask("""document.querySelector('#shelf-settings [data-act="proj:f190"]').click(), true""")
    settle(1200)
    assert browser.ask("document.getElementById('sh-lens').textContent") == "190° fisheye"
    kept = detail(server)
    assert kept["vr_proj"] == 2 and round(kept["vr_lens_fov"]) == 190 and kept["is_vr"]

    browser.ask("""document.querySelector('#shelf-settings [data-act="stereo:2"]').click(), true""")
    settle(1000)
    assert browser.ask("document.getElementById('sh-stereo').textContent") == "3D Over / under"
    assert detail(server)["vr_layout"] == 2


def test_every_lens_can_be_chosen_including_flat(page):
    browser, server = page
    open_the_video(browser, server)
    keys = browser.ask("window.blueshelfDebug.PROJECTIONS.map((p) => p.key)")
    assert keys == ["flat", "e180", "f180", "f190", "f200", "f220", "e360"]
    for key in keys[1:]:
        browser.ask(f"window.blueshelfDebug.act('proj:{key}'), true")
        settle(500)
        label = browser.ask(f"window.blueshelfDebug.PROJECTIONS.find((p) => p.key === '{key}').label")
        assert browser.ask("document.getElementById('sh-lens').textContent") == label
    browser.ask("window.blueshelfDebug.act('proj:flat'), true")
    settle(800)
    assert browser.ask("document.getElementById('vr-stage').hidden"), "a flat film is a plain video on the page"
    assert detail(server)["is_vr"] is False


def test_volume_quality_and_folding_away(page):
    browser, server = page
    open_the_video(browser, server)
    browser.ask("window.blueshelfDebug.act('volume'), true")          # opens the audio tab, as designed
    settle(250)
    browser.ask("""
      (() => { const s = document.querySelector('#shelf-settings [data-slider="volume"]');
               s.value = 250; s.dispatchEvent(new Event('input', { bubbles: true })); return true; })()
    """)
    settle(150)
    assert abs(browser.ask("window.blueshelfDebug.video.volume") - 0.25) < 0.01
    assert browser.ask("document.querySelector('#shelf-settings .st-slider-head span').textContent") == "25%"

    browser.ask("window.blueshelfDebug.act('tab:screen'), true")
    settle(250)
    browser.ask("""document.querySelector('#shelf-settings [data-act="quality:low"]').click(), true""")
    settle(250)
    assert browser.ask("window.blueshelfDebug.state.quality") == "low"
    browser.ask("window.blueshelfDebug.act('quality:auto'), true")

    browser.ask("window.blueshelfDebug.act('collapse'), true")
    settle(250)
    folded = browser.ask("""
      (() => ({ bar: document.getElementById('shelf-bar').hidden,
                pill: !document.getElementById('shelf-collapsed').hidden,
                settings: document.getElementById('shelf-settings').hidden }))()
    """)
    assert folded == {"bar": True, "pill": True, "settings": True}, folded
    browser.ask("document.querySelector('#shelf-collapsed [data-act=\"expand\"]').click(), true")
    settle(250)
    assert not browser.ask("document.getElementById('shelf-bar').hidden")


def test_favourite_and_stars_are_kept_and_delete_asks_twice(page):
    browser, server = page
    open_the_video(browser, server)
    browser.ask("document.querySelector('#shelf-bar [data-act=\"fav\"]').click(), true")
    browser.ask("document.querySelector('#shelf-bar [data-act=\"rate:4\"]').click(), true")
    settle(800)
    kept = detail(server)
    assert kept["favorite"] is True and kept["rating"] == 4
    browser.ask("document.getElementById('sh-delete').click(), true")
    settle(200)
    assert browser.ask("window.blueshelfDebug.state.armed")
    assert not browser.ask("document.getElementById('player').hidden"), "one press never deletes"


# --------------------------------------------------------------------------- the cut-outs

def test_several_cut_outs_can_be_picked_and_the_choice_is_kept(page, tmp_path):
    """One per matting model, say. Which looks best is only clear with the headset on, so they
    are picked while watching, under Environment - and turning them off is remembered too."""
    browser, server = page
    plain = _mask_beside_the_clip(tmp_path)
    shutil.copy(plain, plain.with_name(plain.name.replace(".mask.mp4", ".mask.matanyone2.mp4")))
    open_the_video(browser, server)
    now = lambda: browser.ask("""
      (() => { const d = window.blueshelfDebug; const mesh = d.vr.meshes[0];
               return { masks: d.state.current.masks, name: d.vr.maskName,
                        src: d.vr.maskVideo ? d.vr.maskVideo.src : "",
                        separate: mesh.material.uniforms.separate.value }; })()
    """)
    first = now()
    assert first["masks"] == ["cut-out", "matanyone2"]
    assert first["name"] == "cut-out" and "m=cut-out" in first["src"]

    offered = browser.ask("""
      (async () => { const d = window.blueshelfDebug;
        d.act('tab:env'); d.act('settings');
        await new Promise((r) => setTimeout(r, 200));
        return [...document.querySelectorAll('#shelf-settings [data-act^="mask:"]')].map((b) => b.textContent); })()
    """)
    assert offered == ["cut-out", "matanyone2", "Packed in the film"], offered
    browser.ask("""document.querySelector('#shelf-settings [data-act="mask:matanyone2"]').click(), true""")
    time.sleep(2.5)
    second = now()
    assert second["name"] == "matanyone2" and "m=matanyone2" in second["src"] and second["separate"] == 1
    assert browser.ask("document.querySelectorAll('.mask-source').length") == 1, "the old one is let go of"

    browser.ask("""document.querySelector('#shelf-settings [data-act="mask:off"]').click(), true""")
    time.sleep(1)
    off = now()
    assert off["name"] == "off" and off["src"] == "" and off["separate"] == 0
    open_the_video(browser, server)
    assert now()["name"] == "off", "remembered for this film"


def test_a_film_without_a_cut_out_offers_none_but_still_offers_passthrough(page):
    browser, server = page
    open_the_video(browser, server)
    offered = browser.ask("""
      (async () => { const d = window.blueshelfDebug;
        d.act('tab:env'); d.act('settings'); d.act('passthrough');
        await new Promise((r) => setTimeout(r, 200));
        return { masks: document.querySelectorAll('#shelf-settings [data-act^="mask:"]').length,
                 modes: [...document.querySelectorAll('#shelf-settings [data-act^="see:"]')].map((b) => b.textContent),
                 mode: d.vr.see.mode }; })()
    """)
    assert offered["masks"] == 0
    assert offered["modes"] == ["Cut-out", "Backdrop colour", "Colour only"]
    assert offered["mode"] == 2, "no cut-out anywhere, so the backdrop colour is what it starts with"


def test_passthrough_is_switched_by_the_eye_and_chosen_under_environment(page):
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.buildScene(d.state.current, true);
        const modes = [], worlds = [];
        const seen = () => { modes.push(d.vr.see.mode); worlds.push(d.shelf.env); };
        d.act('passthrough'); seen();                  // the cut-out on, your room behind it
        d.act('see:' + d.SEE_COLOUR); seen();          // a different way of cutting out
        d.act('env:cinema'); seen();                   // a world behind the same cut-out
        d.act('env:passthrough'); seen();              // back to your room, cut-out untouched
        d.act('passthrough'); seen();                  // the cut-out off: the whole film
        return {
          modes, worlds,
          followsTheMeshes: d.vr.meshes.every((m) => m.material.uniforms.seeThrough.value === d.vr.see.mode),
          packedScene: d.isPassthrough({ title: "Kitchen", tags: ["Passthrough"] }),
          packedByName: d.isPassthrough({ title: "Scene_ALPHA", tags: [] }),
          ordinary: d.isPassthrough({ title: "Pool Day", tags: ["POV"] }),
        };
      })()
    """)
    assert result["modes"] == [2, 3, 3, 3, 0], "choosing what is behind never changes the cut-out"
    assert result["worlds"] == ["passthrough", "passthrough", "cinema", "passthrough", "passthrough"]
    assert result["followsTheMeshes"], "both eyes must key the same way"
    assert result["packedScene"] and result["packedByName"] and not result["ordinary"]


# --------------------------------------------------------------------------- the headset

AIMING = """
  const d = window.blueshelfDebug;
  const aim = (part, id, fraction) => {
    const p = d.shelfPoint(part, id, fraction);
    if (!p) return 'not drawn';
    const hand = new d.THREE.Object3D();
    hand.position.set(p.x, p.y, p.z + 0.5);        // just in front, pointing straight at it
    hand.updateMatrixWorld(true);
    const hit = d.aimedAt(hand);
    return hit ? hit : null;
  };
  const setUp = () => {
    d.buildPanel();
    d.vr.panel.position.set(0, 1.5, -1.5);
    d.vr.panel.rotation.set(0, 0, 0);
    d.vr.panel.visible = true;
    d.drawShelfVR(true);
    d.vr.panel.updateMatrixWorld(true);
  };
"""

BAR_CONTROLS = ["play", "prev", "next", "back10", "fwd10", "volume", "threeD", "passthrough",
                "recenter", "settings", "collapse", "fav", "delete", "lensChip"]


def test_every_control_in_the_headset_can_be_aimed_at(page):
    """There are no page buttons inside a headset, so what is drawn and what the ray finds
    have to agree - they are the same list, but the arithmetic between them is not."""
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask(f"""
      (() => {{
        {AIMING}
        setUp();
        const ink = d.vr.ui.bar.surface.canvas.getContext('2d')
                     .getImageData(0, 0, d.vr.ui.bar.surface.canvas.width, d.vr.ui.bar.surface.canvas.height).data;
        let lit = 0;
        for (let i = 3; i < ink.length; i += 4) if (ink[i] > 200) lit++;
        const bar = {{}};
        for (const id of {json.dumps(BAR_CONTROLS)}) {{ const hit = aim('bar', id); bar[id] = hit && hit.id; }}
        const scrub = aim('bar', 'scrub', 0.25);
        d.shelf.settings = true; d.shelf.tab = 'projection'; d.drawShelfVR(true);
        const settings = {{}};
        for (const id of ['tab:projection', 'tab:screen', 'tab:audio', 'tab:env', 'close', 'proj:f190', 'stereo:0']) {{
          const hit = aim('settings', id); settings[id] = hit && hit.id;
        }}
        const barStill = aim('bar', 'play');
        return {{ painted: lit > 50000, bar, scrubId: scrub && scrub.id, scrubAt: scrub && scrub.value,
                  settings, barStill: barStill && barStill.id }};
      }})()
    """)
    assert result["painted"], "the bar should be drawn"
    assert result["bar"] == {c: c for c in BAR_CONTROLS}, result["bar"]
    assert result["scrubId"] == "scrub" and abs(result["scrubAt"] - 0.25) < 0.01
    assert all(k == v for k, v in result["settings"].items()), result["settings"]
    assert result["barStill"] == "play", "settings open above the bar, never over it"


def test_pointing_and_pressing_in_the_headset(page):
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask(f"""
      (() => {{
        {AIMING}
        setUp();
        d.press(aim('bar', 'settings'));
        const opened = d.shelf.settings && d.vr.ui.settings.mesh.visible;
        const barTop = d.vr.ui.bar.mesh.position.y + d.vr.ui.bar.mesh.scale.y / 2;
        d.press(aim('settings', 'tab:screen'));
        const tab = d.shelf.tab;
        d.press(aim('bar', 'delete'));
        const armed = d.state.armed;
        d.press(aim('bar', 'collapse'));
        const folded = d.vr.ui.pill.mesh.visible && !d.vr.ui.bar.mesh.visible && !d.vr.ui.settings.mesh.visible;
        d.press(aim('pill', 'expand'));
        return {{ opened, tab, armed, folded, back: d.vr.ui.bar.mesh.visible,
                  stillOpen: !document.getElementById('player').hidden }};
      }})()
    """)
    assert result["opened"] and result["tab"] == "screen"
    assert result["armed"] and result["stillOpen"], "a stray trigger press must not remove a file"
    assert result["folded"] and result["back"]


def test_the_view_is_set_with_sliders_and_remembered(page):
    """Lying down, or a camera set off to one side, needs the picture moved to suit."""
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.buildScene(d.state.current, true);
        d.resetView();
        d.act('slider:zoom', 1);
        d.act('slider:tilt', 0.8);
        d.act('slider:turn', 0.6);
        const rig = d.vr.rig.rotation;
        return { zoom: d.vr.view.zoom, uniforms: d.vr.meshes.map((m) => m.material.uniforms.zoom.value),
                 pitch: rig.x, yaw: rig.y };
      })()
    """)
    assert result["zoom"] == 3.0 and all(z == 3.0 for z in result["uniforms"]), "both eyes zoom together"
    assert result["pitch"] > 0.1, "tilting up raises the picture"
    assert result["yaw"] < -0.1, "turning right moves the picture right"
    settle(600)                                   # the view is saved a moment after the last change
    browser.go(f"http://127.0.0.1:{server.port}/word/?debug")
    browser.ask("document.querySelector('.card').click()")
    time.sleep(3)
    assert browser.ask("window.blueshelfDebug.vr.view.zoom") == 3.0, "kept for this video"


def test_a_flat_film_gets_a_screen_sized_and_placed_as_asked(page):
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        const item = d.state.current;
        item.is_vr = false; item.vr_layout = 0; item.width = 1600; item.height = 900;
        d.buildScene(item, true);
        d.act('slider:size', 1); d.act('slider:distance', 0);
        const big = { w: d.vr.meshes[0].scale.x, h: d.vr.meshes[0].scale.y, z: d.vr.meshes[0].position.z };
        d.act('btn:resetScreen');
        const reset = { w: d.vr.meshes[0].scale.x, z: d.vr.meshes[0].position.z };
        item.vr_layout = 1;                              // a 3D film for a flat screen
        d.buildScene(item, true);
        const eyes = d.vr.meshes.map((m) => m.layers.mask);
        d.act('threeD');                                 // and seen flat, with one picture for both eyes
        const flat = d.vr.meshes.length;
        d.act('threeD');
        return { big, reset, eyes, flat, width: d.screenWidth() };
      })()
    """)
    assert abs(result["big"]["w"] - 12) < 1e-6 and abs(result["big"]["h"] - 12 * 9 / 16) < 1e-6
    assert abs(result["big"]["z"] + 1.5) < 1e-6
    assert abs(result["reset"]["w"] - 7) < 1e-6
    assert result["eyes"] == [2, 4], "each eye its own half of a side-by-side film"
    assert result["flat"] == 1


def test_worlds_surround_a_film_only_in_the_headset_and_never_over_your_room(page):
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        const item = d.state.current;
        d.buildScene(item, true);
        const named = () => (d.vr.world ? d.vr.world.name : null);
        const out = {};
        d.vr.mode = 'flat'; d.act('env:cinema'); out.onThePage = named();
        d.vr.mode = 'immersive'; d.applyWorld(); out.inTheHeadset = named();
        d.act('env:rooftop'); out.changed = named();
        d.act('passthrough'); out.stillThere = named();        // the cut-out, not the world
        d.act('env:passthrough'); out.withTheRoom = named();   // your own room instead
        d.act('env:rooftop'); out.back = named();
        item.vr_proj = 1; d.buildScene(item, true); out.round360 = named();
        // Behind the film, never in front of it, however near the world's own walls are.
        item.vr_proj = 0; d.buildScene(item, true);
        out.filmOverTheWorld = d.vr.meshes.every((m) => m.material.depthFunc === d.THREE.AlwaysDepth);
        out.worldBehind = d.vr.world.children.every((part) => part.renderOrder < 0);
        d.vr.mode = 'flat'; d.applyWorld();
        return out;
      })()
    """)
    assert result == {"onThePage": None, "inTheHeadset": "world:cinema", "changed": "world:rooftop",
                      "stillThere": "world:rooftop", "withTheRoom": None, "back": "world:rooftop",
                      "round360": None, "filmOverTheWorld": True, "worldBehind": True}, result


def test_the_headset_panels_controls_never_overlap_or_spill(page):
    """A control half under another is one the ray picks wrongly, and one past the panel's edge
    is one nobody can see to point at."""
    browser, server = page
    open_the_video(browser, server)
    trouble = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.buildPanel(); d.vr.panel.visible = true;
        const problems = [];
        const check = (name, width, height) => {
          const regions = d.vr.ui[name].surface.regions;
          for (const r of regions) {
            if (r.x < -0.5 || r.y < -0.5 || r.x + r.w > width + 0.5 || r.y + r.h > height + 0.5) problems.push(`${name}: ${r.id} spills`);
          }
          for (let i = 0; i < regions.length; i++) for (let j = i + 1; j < regions.length; j++) {
            const a = regions[i], b = regions[j];
            if (a.x < b.x + b.w - 0.5 && b.x < a.x + a.w - 0.5 && a.y < b.y + b.h - 0.5 && b.y < a.y + a.h - 0.5) {
              problems.push(`${name}: ${a.id} overlaps ${b.id}`);
            }
          }
        };
        for (const leave of [false, true]) {
          d.vr.mode = leave ? 'immersive' : 'flat';
          d.shelf.settings = false; d.drawShelfVR(true);
          check('bar', d.ShelfVR.BAR.w, d.ShelfVR.BAR.h);
        }
        for (let i = 0; i < 8; i++) d.shelf.scenes.push({ id: 200 + i, label: `A scene with a long name ${i}`, start: 10, end: 99 });
        for (const tab of ['projection', 'screen', 'audio', 'env', 'scenes']) {
          d.shelf.settings = true; d.shelf.tab = tab;
          if (tab === 'env') d.act('passthrough');
          d.drawShelfVR(true);
          check('settings', d.ShelfVR.SETTINGS.w, d.vr.ui.settings.surface.shown);
        }
        d.vr.mode = 'flat';
        return problems;
      })()
    """)
    assert trouble == [], trouble


# --------------------------------------------------------------------------- scenes

def test_a_scene_is_marked_while_watching_and_can_be_moved_and_removed(page):
    """Press once where it starts, again where it ends: kept on the PC, shown under the bar."""
    browser, server = page
    open_the_video(browser, server)
    browser.ask("window.blueshelfDebug.video.pause(), window.blueshelfDebug.video.currentTime = 1.0, true")
    settle(400)
    browser.ask("document.getElementById('sh-mark').click(), true")
    settle(200)
    assert browser.ask("document.getElementById('sh-mark').textContent") == "End scene · 0:01"
    browser.ask("window.blueshelfDebug.video.currentTime = 4.5, true")
    settle(400)
    browser.ask("document.getElementById('sh-mark').click(), true")
    settle(800)
    kept = detail(server)["scenes"]
    assert len(kept) == 1 and abs(kept[0]["start"] - 1.0) < 0.2 and abs(kept[0]["end"] - 4.5) < 0.2, kept
    assert browser.ask("document.querySelectorAll('#sh-scenes .sh-scene').length") == 1
    assert browser.ask("document.getElementById('sh-mark').textContent") == "Mark scene"

    browser.ask("window.blueshelfDebug.act('tab:scenes'), window.blueshelfDebug.act('settings'), true")
    settle(300)
    listed = browser.ask("[...document.querySelectorAll('#shelf-settings .st-scene-name')].map((n) => n.textContent)")
    assert listed == ["Scene 1"]
    browser.ask("window.blueshelfDebug.video.currentTime = 5.5, true")
    settle(400)
    browser.ask(f"document.querySelector('#shelf-settings [data-act=\"scene-end:{kept[0]['id']}\"]').click(), true")
    settle(800)
    assert abs(detail(server)["scenes"][0]["end"] - 5.5) < 0.2, "End here moves the end to where the film is"
    browser.ask(f"document.querySelector('#shelf-settings [data-act=\"scene-del:{kept[0]['id']}\"]').click(), true")
    settle(800)
    assert detail(server)["scenes"] == []
    assert browser.ask("document.querySelectorAll('#sh-scenes .sh-scene').length") == 0


def test_a_scene_can_be_marked_from_the_headset_and_its_list_fits(page):
    browser, server = page
    open_the_video(browser, server)
    result = browser.ask(f"""
      (async () => {{
        {AIMING}
        setUp();
        d.video.pause(); d.video.currentTime = 0.5;
        await new Promise((r) => setTimeout(r, 400));
        const mark = aim('bar', 'mark');
        d.press(mark);
        const started = d.shelf.marking != null;
        d.video.currentTime = 4; await new Promise((r) => setTimeout(r, 400));
        d.press(aim('bar', 'mark'));
        await new Promise((r) => setTimeout(r, 800));
        const kept = d.shelf.scenes.length;
        // Seven more, to fill the list past what fits.
        for (let i = 0; i < 7; i++) d.shelf.scenes.push({{ id: 100 + i, label: `Scene ${{i + 2}}`, start: 0, end: 3 }});
        d.shelf.settings = true; d.shelf.tab = 'scenes'; d.drawShelfVR(true);
        const ids = d.vr.ui.settings.surface.regions.map((r) => r.id);
        return {{ mark: mark && mark.id, started, kept, rows: ids.filter((i) => i.startsWith('scene-go:')).length,
                  shown: d.vr.ui.settings.surface.shown }};
      }})()
    """)
    assert result["mark"] == "mark" and result["started"] and result["kept"] == 1
    assert result["rows"] == 6, "as many as fit; the rest are on the PC"
    assert result["shown"] <= 560


def test_the_cut_out_edge_is_soft_not_a_staircase(page, tmp_path):
    """A mask is a fraction of the film's size, so its edge arrives as a staircase. Cutting at a
    hard threshold turned that into spikes radiating off the performer."""
    browser, server = page
    clip = tmp_path / "library" / "TestVR.Anna.Bell.180_LR.mp4"
    subprocess.run([paths.tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=red:s=1280x640:r=10:d=6", "-pix_fmt", "yuv420p",
                    "-c:v", "h264_nvenc", "-t", "6", str(clip)], capture_output=True, timeout=180, check=True)
    # A quarter-size mask: kept on the left of each eye, cut away on the right.
    mask = clip.with_name(clip.stem + ".mask.mp4")
    subprocess.run([paths.tool("ffmpeg"), "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=white:s=80x160:r=10:d=6",
                    "-f", "lavfi", "-i", "color=c=black:s=80x160:r=10:d=6",
                    "-filter_complex", "[0:v][1:v]hstack,split[a][b];[a][b]hstack",
                    "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-t", "6", str(mask)],
                   capture_output=True, timeout=180, check=True)
    open_the_video(browser, server)
    time.sleep(2)
    edge = browser.ask("""
      (() => {
        const d = window.blueshelfDebug;
        d.act('see:' + d.SEE_MASK);
        d.buildScene(d.state.current, false);
        d.vr.camera.layers.set(0);
        d.vr.camera.rotation.set(0, 0, 0);
        d.vr.renderer.render(d.vr.scene, d.vr.camera);
        const gl = d.vr.renderer.getContext();
        const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
        const row = new Uint8Array(w * 4);
        gl.readPixels(0, Math.floor(h / 2), w, 1, gl.RGBA, gl.UNSIGNED_BYTE, row);
        const reds = [];
        for (let x = 0; x < w; x++) reds.push(row[x * 4]);
        const kept = reds.filter((r) => r > 250).length;
        const gone = reds.filter((r) => r < 5).length;
        const between = reds.filter((r) => r >= 5 && r <= 250).length;
        const at = reds.findIndex((r, i) => i && Math.abs(r - reds[i - 1]) > 40);
        return { kept, gone, between, mask: d.vr.maskState, step: d.vr.meshes[0].material.uniforms.maskStep.value,
                 across: reds.slice(Math.max(0, at - 3), at + 4) };
      })()
    """)
    assert edge["mask"] == "on", edge
    assert edge["kept"] > 20 and edge["gone"] > 20, f"the mask should keep one side and cut the other: {edge}"
    # One sample and a narrow threshold gave 254, 38, 0 - a single step, which reads in a headset
    # as spikes radiating off the performer. Five samples over a wider band give a real fade.
    assert edge["between"] >= 3, f"the edge fades over several pixels, not one: {edge}"
    # The mask is 320 across: two 160-wide eyes, each half kept and half cut.
    assert abs(edge["step"]["x"] - 1 / 320) < 1e-6, "one pixel of the mask, measured from the mask itself"
