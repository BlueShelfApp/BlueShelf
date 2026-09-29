// BlueShelf in a headset browser: browse by performer, watch, and delete what you are watching.
// The VR view reprojects one eye of a 180/360/fisheye video, matching blueshelf/shaders/vr.glsl.
import * as THREE from "./three.module.min.js";
import { iconSvg } from "./icons.js";
import * as ShelfVR from "./shelf-vr.js";
import { WORLD_CARDS, buildWorld, disposeWorld } from "./worlds.js";

const KEY = new URLSearchParams(location.search).get("k") || "";
// The page may be served under the address word - http://pc:8760/word/ - so every address the
// server hands back ("/video/12") is asked for under that same word.
const BASE = location.pathname.replace(/[^/]*$/, "").replace(/\/$/, "");
const at = (path) => (path.startsWith("/") ? BASE + path : path);
const LAYOUT_MONO = 0, LAYOUT_SBS = 1, LAYOUT_TB = 2;
const PROJ_180 = 0, PROJ_360 = 1, PROJ_FISHEYE = 2;

const state = { videos: [], view: "all", performer: null, search: "", current: null, index: -1, eye: 0,
                armed: false, quality: "auto", offset: 0, converting: false, forced: false };
const QUALITY_KEY = "blueshelf-quality";
const QUALITY_ORDER = ["auto", "original", "high", "medium", "low"];
const PLAYABLE_CONTAINERS = ["mp4", "m4v", "webm", "mov"];
// A locked-down browser - private mode, or cookies and storage switched off - throws when the
// page touches localStorage. Remembering the volume is not worth losing the whole library over.
const remember = {
  get(key, fallback = null) {
    try { return localStorage.getItem(key) ?? fallback; } catch (err) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch (err) { /* nothing to remember with */ }
  },
};

const el = (id) => document.getElementById(id);

function api(path, options = {}) {
  const join = path.includes("?") ? "&" : "?";
  return fetch(KEY ? `${at(path)}${join}k=${encodeURIComponent(KEY)}` : at(path), options);
}

function toast(message, ms = 2600) {
  const box = el("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, ms);
}

const clock = (seconds) => {
  if (!seconds || seconds < 0) return "0:00";
  const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const rest = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${rest}` : `${m}:${rest}`;
};

function quality(video) {
  if (!video.width || !video.height) return "";
  const long = Math.max(video.width, video.height), short = Math.min(video.width, video.height);
  if (video.is_vr) return `${Math.max(1, Math.round(long / 1000))}K`;
  if (long >= 3800 || short >= 2000) return "4K";
  if (long >= 1900 || short >= 1000) return "1080p";
  if (long >= 1260 || short >= 700) return "720p";
  return `${short}p`;
}

// ---------------------------------------------------------------- library

async function loadPerformers() {
  const performers = await (await api("/api/performers")).json();
  const box = el("performers");
  box.innerHTML = "";
  for (const person of performers) {
    const button = document.createElement("button");
    button.className = "side-item";
    button.innerHTML = `<span>${person.name}</span><span class="n">${person.count}</span>`;
    button.onclick = () => {
      state.performer = state.performer === person.id ? null : person.id;
      state.view = "all";
      for (const item of document.querySelectorAll(".side-item")) item.classList.remove("active");
      if (state.performer) button.classList.add("active");
      loadLibrary();
    };
    box.appendChild(button);
  }
}

// The list is fetched again now and then, so a scene that finished downloading turns up without
// anyone reaching for refresh. The grid is only rebuilt when the list really changed, so a quiet
// check cannot jump the scroll position or interrupt anything.
function signature(videos) {
  return videos.map((video) => `${video.id}:${video.title}:${video.favorite ? 1 : 0}:${video.rating}`).join(",");
}

async function loadLibrary(quiet = false) {
  const params = new URLSearchParams({ view: state.view });
  if (state.performer) params.set("performer", state.performer);
  if (state.search) params.set("search", state.search);
  let fetched;
  try {
    fetched = await (await api(`/api/library?${params}`)).json();
  } catch (error) {
    if (quiet) return;  // a moment without the PC is not worth emptying the page over
    throw error;
  }
  const grid = el("grid");
  if (quiet && signature(fetched) === signature(state.videos)) return;
  state.videos = fetched;
  const scrolled = grid.scrollTop;
  grid.innerHTML = "";
  el("count").textContent = `${state.videos.length} videos`;
  for (const video of state.videos) {
    const card = document.createElement("div");
    card.className = "card";
    const image = video.thumb ? `<img loading="lazy" src="${at(video.thumb)}${KEY ? `?k=${KEY}` : ""}" alt="">` : "";
    const badges = [
      video.is_vr ? '<span class="badge vr">VR</span>' : "",
      quality(video) ? `<span class="badge quality">${quality(video)}</span>` : "",
      video.duration ? `<span class="badge time">${clock(video.duration)}</span>` : "",
    ].join("");
    card.innerHTML = `<div class="thumb">${image}${badges}</div>
      <h3>${video.favorite ? "♥ " : ""}${video.title}</h3>
      <p>${[video.performers.join(", "), video.studio].filter(Boolean).join(" · ") || "&nbsp;"}</p>`;
    card.onclick = () => openVideo(video);
    grid.appendChild(card);
  }
  grid.scrollTop = scrolled;  // a quiet refresh must not throw you back to the top
}

// ---------------------------------------------------------------- player

const video = el("video");

function openVideo(item) {
  state.current = item;
  state.index = state.videos.findIndex((video) => video.id === item.id);
  el("side-prev").disabled = state.index <= 0;
  el("side-next").disabled = state.index < 0 || state.index >= state.videos.length - 1;
  state.armed = false;
  Object.assign(shelf, { settings: false, collapsed: false, scrubHover: null, strip: null, stillsNote: "", chapters: [],
                         scenes: [], marking: null });
  el("player").hidden = false;
  vrSupported().then((yes) => { el("sh-vr").hidden = !yes; });
  showSurface(item);
  state.forced = false;
  video.volume = Number(remember.get(VOLUME_KEY, 1));
  loadSource(item.resume > 30 ? item.resume : 0);
  attachMask(item);
  loadStrip(item);
  requestShelf();
}

// Browsers vary in what they can decode: HEVC is common on a PC and rare on a headset, and MKV plays
// nowhere. Anything the browser will not take is converted by BlueShelf as it streams.
function browserCanPlay(item) {
  if (!PLAYABLE_CONTAINERS.includes(item.container)) return false;
  const probe = document.createElement("video");
  const codec = item.codec || "";
  if (codec.includes("hevc") || codec.includes("h265")) {
    return Boolean(probe.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
                   probe.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"'));
  }
  if (codec.includes("av1")) return Boolean(probe.canPlayType('video/mp4; codecs="av01.0.05M.08"'));
  if (codec.includes("vp9")) return Boolean(probe.canPlayType('video/webm; codecs="vp9"'));
  return true;
}

function totalLength() {
  return (state.current && state.current.duration) || video.duration || 0;
}

function loadSource(seconds = 0) {
  const item = state.current;
  if (!item) return;
  const chosen = state.quality;
  const convert = state.forced || (chosen === "original" ? false
    : chosen === "auto" ? !browserCanPlay(item) : true);
  state.converting = convert;
  state.offset = convert ? seconds : 0;
  const key = KEY ? `k=${encodeURIComponent(KEY)}` : "";
  if (convert) {
    const level = ["high", "medium", "low"].includes(chosen) ? chosen : "high";
    video.src = `${at(item.convert)}?q=${level}&seek=${Math.floor(seconds)}&${key}`;
  } else {
    video.src = `${at(item.video)}?${key}`;
  }
  video.load();
  followFrames(video, "filmShown");
  if (!convert && seconds) video.currentTime = seconds;
  video.play().catch(() => toast("Tap play to start"));
  requestShelf();
}

function seekTo(seconds) {
  const target = Math.max(0, Math.min(seconds, totalLength() || seconds));
  if (state.converting) loadSource(target);
  else video.currentTime = target;
}

async function step(delta) {
  if (state.index < 0) return;
  const next = state.videos[state.index + delta];
  if (!next) {
    toast(delta > 0 ? "Last video in this list" : "First video in this list");
    return;
  }
  await saveResume();
  openVideo(next);
}

async function closePlayer() {
  await saveResume();
  leaveVR();
  stopFlat();
  detachMask();
  video.pause();
  video.removeAttribute("src");
  video.load();
  el("player").hidden = true;
  state.current = null;
  clearTimeout(loadStrip.timer);
  Object.assign(shelf, { settings: false, collapsed: false, scrubHover: null, strip: null, stillsNote: "", chapters: [],
                         scenes: [], marking: null });
  loadLibrary();
}

async function saveResume() {
  const total = totalLength();
  if (!state.current || !total) return;
  const at = state.offset + video.currentTime;
  const keep = at > 30 && at < total - 60 ? at : 0;
  await api(`/api/video/${state.current.id}/resume`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: keep }),
  });
}

// ---------------------------------------------------------------- the Shelf controls
// One description of the controls - what is playing, how far in, how it is shaped, what is
// switched on - drawn twice: as HTML on the page, and onto canvases inside the headset. Both
// send what is pressed to act(), so a button does the same thing wherever it is pressed.

const VOLUME_KEY = "blueshelf-volume";
const SHELF_KEY = "blueshelf-shelf";

// "passthrough" as the setting behind the film means your real room; anything else is a world.
const shelf = { tab: "projection", env: "passthrough", screenSize: 0.5, screenDistance: 0.35, threeD: true };
try { Object.assign(shelf, JSON.parse(remember.get(SHELF_KEY) || "{}")); } catch (error) { /* defaults */ }
// Never carried over from a visit before: nobody expects a film to open with settings showing.
Object.assign(shelf, { settings: false, collapsed: false, scrubHover: null, hover: null, strip: null,
                       chapters: [], scenes: [], marking: null });

function keepShelf() {
  remember.set(SHELF_KEY, JSON.stringify({ tab: shelf.tab, env: shelf.env, screenSize: shelf.screenSize,
                                           screenDistance: shelf.screenDistance, threeD: shelf.threeD }));
}

// The lens choices, as the design lays them out - plus 180 degree fisheye, which the design
// leaves out and every DeoVR passthrough copy BlueShelf makes is.
const PROJECTIONS = [
  { key: "flat", label: "Flat", sub: "Standard screen", deg: 0 },
  { key: "e180", label: "180°", sub: "VR180 equirect", deg: 180, proj: PROJ_180, fov: 180 },
  { key: "f180", label: "180° fisheye", sub: "For passthrough", deg: 180, proj: PROJ_FISHEYE, fov: 180 },
  { key: "f190", label: "190° fisheye", sub: "Dual-fisheye rigs", deg: 190, proj: PROJ_FISHEYE, fov: 190 },
  { key: "f200", label: "200° fisheye", sub: "Wide fisheye", deg: 200, proj: PROJ_FISHEYE, fov: 200 },
  { key: "f220", label: "220° fisheye", sub: "Ultra-wide", deg: 220, proj: PROJ_FISHEYE, fov: 220 },
  { key: "e360", label: "360°", sub: "Full sphere", deg: 360, proj: PROJ_360, fov: 360 },
];
const STEREOS = [
  { key: LAYOUT_MONO, label: "2D", chip: "2D" },
  { key: LAYOUT_SBS, label: "Side by side", chip: "3D Side by side" },
  { key: LAYOUT_TB, label: "Over / under", chip: "3D Over / under" },
];
// A function, not a list: the see-through modes are defined further down the file.
const seeChoices = () => [
  { key: SEE_MASK, label: "Cut-out" },
  { key: SEE_BACKDROP, label: "Backdrop colour" },
  { key: SEE_COLOUR, label: "Colour only" },
];

function projectionOf(item) {
  if (!item || !item.is_vr) return PROJECTIONS[0];
  if (item.vr_proj === PROJ_360) return PROJECTIONS[6];
  if (item.vr_proj !== PROJ_FISHEYE) return PROJECTIONS[1];
  const fov = Math.round(item.vr_lens_fov || 190);
  return PROJECTIONS.find((p) => p.proj === PROJ_FISHEYE && p.fov === fov)
      || { key: `f${fov}`, label: `${fov}° fisheye`, sub: "", deg: fov, proj: PROJ_FISHEYE, fov };
}

function lensName(item) {
  return projectionOf(item).label;
}

function chapterAt(seconds) {
  let name = "";
  for (const chapter of shelf.chapters) if (seconds >= chapter.start) name = chapter.title;
  return name;
}

function bufferedShare() {
  const total = totalLength();
  if (!total || !video.buffered || !video.buffered.length) return 0;
  const now = video.currentTime;
  for (let i = 0; i < video.buffered.length; i++) {
    if (video.buffered.start(i) <= now + 0.5 && video.buffered.end(i) >= now) {
      return Math.min(1, (state.offset + video.buffered.end(i)) / total);
    }
  }
  return 0;
}

// The flat screen a film that is not VR is shown on in the headset.
const screenWidth = () => 2 + shelf.screenSize * 10;             // metres
const screenDistance = () => 1.5 + shelf.screenDistance * 8.5;

function screenModel(item) {
  if (item && item.is_vr) {
    const view = vr.view;
    const turn = Math.round(-view.yaw / DEG), tilt = Math.round(view.pitch / DEG);
    return {
      sliders: [
        { key: "zoom", label: "Zoom", valueLabel: `${view.zoom.toFixed(2)}×`,
          value: (view.zoom - ZOOM_MIN) / (ZOOM_MAX - ZOOM_MIN) },
        { key: "tilt", label: "Tilt", valueLabel: `${tilt}°`, value: (view.pitch + TILT_LIMIT) / (2 * TILT_LIMIT) },
        { key: "turn", label: "Turn", valueLabel: `${turn}°`, value: 0.5 - Math.max(-0.5, Math.min(0.5, view.yaw / (2 * Math.PI))) },
      ],
      resetLabel: "Reset view",
      quality: qualityChoices(),
      hint: vr.mode === "immersive" ? "Grip drags the view · A plays · X, B or a stick press hides this" : "",
    };
  }
  return {
    sliders: [
      { key: "size", label: "Screen size", valueLabel: `${Math.round(screenWidth() * 3.28084)} ft wide`, value: shelf.screenSize },
      { key: "distance", label: "Distance", valueLabel: `${screenDistance().toFixed(1)} m`, value: shelf.screenDistance },
    ],
    resetLabel: "Reset size",
    quality: qualityChoices(),
    hint: "",
  };
}

function qualityChoices() {
  const names = { auto: "Auto", original: "Original", high: "High", medium: "Medium", low: "Low" };
  return QUALITY_ORDER.map((key) => ({ key, label: names[key], on: state.quality === key }));
}

function shelfModel() {
  const item = state.current;
  const total = totalLength();
  const now = shownTime();
  const proj = projectionOf(item);
  const stereo = STEREOS.find((s) => s.key === (item ? item.vr_layout : LAYOUT_MONO)) || STEREOS[0];
  const seeing = vr.see.mode !== SEE_OFF;
  const who = item ? [item.performers.join(", "), item.studio].filter(Boolean).join(" · ") : "";
  const hoverAt = shelf.scrubHover == null ? 0 : shelf.scrubHover * total;
  return {
    title: item ? item.title : "",
    subtitle: chapterAt(now) || who,
    lens: item ? proj.label : "",
    stereo: item ? stereo.chip : "",
    cur: clock(now), total: clock(total), remain: clock(Math.max(0, total - now)),
    progress: total ? Math.min(1, now / total) : 0,
    buffered: bufferedShare(),
    ticks: total ? shelf.chapters.filter((c) => c.start > 1).map((c) => c.start / total) : [],
    scenes: shelf.scenes.map((s) => ({
      id: s.id, label: s.label, from: total ? s.start / total : 0, to: total ? s.end / total : 0,
      times: `${clock(s.start)} – ${clock(s.end)} · ${clock(s.end - s.start)}`,
    })),
    marking: shelf.marking == null ? null
      : { from: total ? Math.min(shelf.marking, now) / total : 0, to: total ? Math.max(shelf.marking, now) / total : 0,
          at: clock(shelf.marking) },
    playing: !video.paused,
    volume: video.volume, muted: video.muted || video.volume === 0,
    threeD: shelf.threeD, threeDAvailable: Boolean(item && item.vr_layout !== LAYOUT_MONO),
    passthrough: seeing,
    settings: shelf.settings, tab: shelf.tab, collapsed: shelf.collapsed,
    fav: Boolean(item && item.favorite), armed: state.armed, rating: item ? item.rating : 0,
    leave: vr.mode === "immersive",
    hover: shelf.hover,
    scrubHover: shelf.scrubHover,
    hoverTime: clock(hoverAt), hoverChapter: chapterAt(hoverAt) || "",
    stillsNote: shelf.strip ? "" : (shelf.stillsNote || "Making stills…"),
    projections: PROJECTIONS.map((p) => ({ ...p, on: p.key === proj.key })),
    stereos: STEREOS.map((s) => ({ key: s.key, label: s.label, on: s.key === stereo.key })),
    screen: screenModel(item),
    envs: WORLD_CARDS.map((w) => ({ key: w.key, label: w.label, hint: w.hint, swatch: w.swatch, swatchFg: w.fg,
                                    on: shelf.env === w.key })),
    passthroughOptions: seeing ? {
      modes: seeChoices().map((c) => ({ ...c, on: vr.see.mode === c.key })),
      masks: item && (item.masks || []).length
        ? [...item.masks.map((name) => ({ key: name, label: name, on: vr.maskName === name })),
           { key: "off", label: "Packed in the film", on: vr.maskName === "off" }]
        : [],
    } : null,
  };
}

// ---------------------------------------------------------------- what the buttons do

function act(id, value) {
  const split = id.indexOf(":");
  const kind = split < 0 ? id : id.slice(0, split);
  const key = split < 0 ? "" : id.slice(split + 1);
  switch (kind) {
    case "play": if (video.paused) video.play().catch(() => {}); else video.pause(); break;
    case "prev": step(-1); break;
    case "next": step(1); break;
    case "back10": seekTo(state.offset + video.currentTime - 10); break;
    case "fwd10": seekTo(state.offset + video.currentTime + 10); break;
    case "seek": { const total = totalLength(); if (total) seekTo(value * total); break; }
    case "volume": openTab("audio"); break;
    case "mute": setMuted(!video.muted); break;
    case "slider": setSlider(key, value); break;
    case "threeD": setThreeD(!shelf.threeD); break;
    case "passthrough": setSee(vr.see.mode === SEE_OFF ? preferredSee(state.current) : SEE_OFF); break;
    case "recenter": recenter(); break;
    case "settings": shelf.settings = !shelf.settings; shelf.collapsed = false; break;
    case "close": shelf.settings = false; break;
    case "close-player": closePlayer(); break;
    case "collapse": shelf.collapsed = true; shelf.settings = false; break;
    case "expand": shelf.collapsed = false; break;
    case "lensChip": openTab("projection"); break;
    case "tab": shelf.tab = key; keepShelf(); break;
    case "proj": setProjection(key); break;
    case "stereo": setStereo(Number(key)); break;
    case "quality": setQuality(key); break;
    case "env": setWorld(key); break;
    case "see": setSee(Number(key)); break;
    case "mask": setMask(key); break;
    case "btn": if (key === "recenter") recenter(); else if (key === "resetScreen") resetScreen(); break;
    case "fav": toggleFavorite(); break;
    case "delete": deleteCurrent(); break;
    case "rate": rate(Number(key)); break;
    case "full": fullScreen(); break;
    case "vr": enterVR(); break;
    case "leave": leaveVR(); break;
    case "mark": markScene(); break;
    case "scene-go": { const s = shelf.scenes.find((x) => String(x.id) === key); if (s) seekTo(s.start); break; }
    case "scene-start": changeScene(key, { start: shownTime() }); break;
    case "scene-end": changeScene(key, { end: shownTime() }); break;
    case "scene-del": changeScene(key, null); break;
    default: return;
  }
  requestShelf();
}

// Scenes: press once where one starts, again where it ends. They are made into passthrough
// copies on the PC, where they can be put in order and trimmed; here they are only marked.
async function markScene() {
  const item = state.current;
  if (!item) return;
  const now = shownTime();
  if (shelf.marking == null) {
    shelf.marking = now;
    toast(`Scene started at ${clock(now)} - press Mark again where it ends`, 3000);
    requestShelf();
    return;
  }
  const start = shelf.marking;
  shelf.marking = null;
  requestShelf();
  await sceneRequest(item, "scene-add", { start, end: now }, (scenes) => {
    const made = scenes.find((s) => Math.abs(s.start - Math.min(start, now)) < 0.01);
    toast(`${made ? made.label : "Scene"} kept: ${clock(Math.min(start, now))} – ${clock(Math.max(start, now))}`, 2500);
  });
}

async function changeScene(id, change) {
  const item = state.current;
  if (!item) return;
  if (change === null) await sceneRequest(item, "scene-delete", { id: Number(id) }, () => toast("Scene removed", 1500));
  else await sceneRequest(item, "scene-change", { id: Number(id), ...change }, () => toast("Scene moved", 1200));
}

async function sceneRequest(item, action, body, done) {
  let told = null;
  try {
    told = await (await api(`/api/video/${item.id}/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json();
  } catch (error) {
    toast("The PC did not answer - the scene was not kept", 3000);
    return;
  }
  if (state.current !== item) return;
  shelf.scenes = told.scenes || shelf.scenes;
  if (told.ok) done(shelf.scenes);
  else toast(told.error || "That scene could not be kept", 3000);
  requestShelf();
}

function openTab(tab) {
  shelf.settings = true;
  shelf.collapsed = false;
  shelf.tab = tab;
  keepShelf();
}

function setMuted(muted) {
  video.muted = muted;
  requestShelf();
}

function setVolume(level, quietly = false) {
  video.volume = Math.max(0, Math.min(1, level));
  video.muted = false;
  remember.set(VOLUME_KEY, String(video.volume));
  if (!quietly) toast(`Volume ${Math.round(video.volume * 100)}%`, 900);
  requestShelf();
}

function setSlider(key, value) {
  const v = Math.max(0, Math.min(1, value));
  if (key === "volume") setVolume(v, true);
  else if (key === "zoom") { vr.view.zoom = ZOOM_MIN + v * (ZOOM_MAX - ZOOM_MIN); applyView(); saveView(); }
  else if (key === "tilt") { vr.view.pitch = -TILT_LIMIT + v * 2 * TILT_LIMIT; applyView(); saveView(); }
  else if (key === "turn") { vr.view.yaw = (0.5 - v) * 2 * Math.PI; applyView(); saveView(); }
  else if (key === "size") { shelf.screenSize = v; keepShelf(); placeScreen(); }
  else if (key === "distance") { shelf.screenDistance = v; keepShelf(); placeScreen(); }
}

// Pointed the other way from where you are facing now, as the headset's own recentre does. The
// zoom stays: somebody who zoomed in meant it.
function recenter() {
  vr.view.yaw = vr.view.pitch = vr.view.roll = 0;
  vr.yaw = vr.pitch = 0;
  applyView();
  saveView();
  if (vr.mode === "immersive" && vr.shelfGroup && vr.shelfGroup.visible) placePanel();
  toast("Centered", 900);
}

function resetScreen() {
  if (state.current && state.current.is_vr) {
    resetView();
  } else {
    shelf.screenSize = 0.5;
    shelf.screenDistance = 0.35;
    keepShelf();
    placeScreen();
  }
}

function setThreeD(on) {
  shelf.threeD = on;
  keepShelf();
  if (vr.renderer && state.current && vr.item === state.current) buildScene(state.current, vr.stereo);
  toast(on ? "3D - each eye its own picture" : "2D - both eyes the same picture", 1500);
}

function preferredSee(item) {
  return (item && ((item.masks || []).length || isPassthrough(item))) ? SEE_MASK : SEE_BACKDROP;
}

// The cut-out, on or off. What shows through it is chosen separately, under Environment:
// your room, or one of the worlds.
function setSee(mode) {
  vr.see.mode = mode;
  if (mode === SEE_BACKDROP) sampleBackdrop();
  applySeeThrough();
  applyWorld();
  if (mode !== SEE_OFF && shelf.env === "passthrough" && vr.mode === "immersive" && !vr.inAR) roomLater();
}

function setWorld(key) {
  shelf.env = key;
  keepShelf();
  applyWorld();
  if (key === "passthrough") {
    // The room only means anything round a cut-out, so choosing it turns one on.
    if (vr.see.mode === SEE_OFF) setSee(preferredSee(state.current));
    else if (vr.mode === "immersive" && !vr.inAR) roomLater();
  }
}

function roomLater() {
  toast("Leave VR and tap VR again to see your room behind it", 4000);
}

function setQuality(key) {
  if (!QUALITY_ORDER.includes(key)) return;
  state.quality = key;
  state.forced = false;
  remember.set(QUALITY_KEY, key);
  const explain = { auto: "Convert only when this device cannot play the original", original: "Always the original file",
                    high: "Converted, high quality", medium: "Converted, medium", low: "Converted, low" };
  toast(explain[key]);
  loadSource(state.offset + (video.currentTime || 0));
}

async function setProjection(key) {
  const item = state.current;
  const choice = PROJECTIONS.find((p) => p.key === key);
  if (!item || !choice) return;
  if (choice.key === "flat") {
    item.is_vr = false;
  } else {
    item.is_vr = true;
    item.vr_proj = choice.proj;
    item.vr_lens_fov = choice.fov;
  }
  reshape(item);
  toast(`Lens: ${choice.label}`);
  await saveShape(item);
}

async function setStereo(layout) {
  const item = state.current;
  if (!item || !STEREOS.some((s) => s.key === layout)) return;
  item.vr_layout = layout;
  reshape(item);
  toast(`Stereo: ${STEREOS.find((s) => s.key === layout).label}`);
  await saveShape(item);
}

// Redrawn at once, so a lens can be judged by looking - the only way to judge one.
function reshape(item) {
  if (vr.mode === "immersive") buildScene(item, true);
  else showSurface(item);
  shelf.strip = null;
  loadStrip(item);
}

async function saveShape(item) {
  await api(`/api/video/${item.id}/lens`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ flat: !item.is_vr, proj: item.vr_proj || 0, fov: item.vr_lens_fov || 180,
                           layout: item.vr_layout || 0 }),
  });
}

async function toggleFavorite() {
  const item = state.current;
  if (!item) return;
  item.favorite = !item.favorite;
  requestShelf();
  await api(`/api/video/${item.id}/favorite`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: item.favorite }),
  });
}

async function rate(stars) {
  const item = state.current;
  if (!item) return;
  item.rating = stars === item.rating ? 0 : stars;
  requestShelf();
  await api(`/api/video/${item.id}/rating`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: item.rating }),
  });
}

// Two presses to delete: the first arms the button, so a stray press in a headset cannot
// remove a file.
async function deleteCurrent() {
  const item = state.current;
  if (!item) return;
  if (!state.armed) {
    state.armed = true;
    toast("Press delete again to delete this film", 3500);
    clearTimeout(deleteCurrent.timer);
    deleteCurrent.timer = setTimeout(() => { state.armed = false; requestShelf(); }, 4000);
    return;
  }
  state.armed = false;
  const answer = await (await api(`/api/video/${item.id}/delete`, { method: "POST" })).json();
  if (answer.ok) {
    toast(answer.permanent ? "Deleted" : "Moved to the trash on your PC");
    video.pause();
    await closePlayer();
  } else {
    toast(answer.error || "Could not delete that file");
  }
}

function fullScreen() {
  const player = el("player");
  if (document.fullscreenElement) document.exitFullscreen();
  else if (player.requestFullscreen) player.requestFullscreen().catch(() => toast("Full screen was refused"));
}

// ---------------------------------------------------------------- the stills and the chapters

async function loadStrip(item) {
  clearTimeout(loadStrip.timer);
  if (!item || state.current !== item) return;
  let told = null;
  try {
    told = await (await api(`/api/strip/${item.id}`)).json();
  } catch (error) { return; }
  if (state.current !== item || !told) return;
  if (told.ready) {
    const image = new Image();
    image.onload = () => {
      if (state.current !== item) return;
      shelf.stillsNote = "";
      shelf.strip = { image, url: image.src, count: told.count, cols: told.cols, tileW: told.tileW, tileH: told.tileH };
      requestShelf();
    };
    image.src = `${at(told.url)}?v=${Date.now()}${KEY ? `&k=${encodeURIComponent(KEY)}` : ""}`;
  } else if (told.making) {
    shelf.stillsNote = told.count ? `Making stills… ${Math.round((100 * (told.done || 0)) / told.count)}%`
                                  : "Making stills…";
    requestShelf();
    loadStrip.timer = setTimeout(() => loadStrip(item), 3000);   // being made; look again shortly
  } else {
    shelf.stillsNote = "No stills for this film";
    requestShelf();
  }
}

// ---------------------------------------------------------------- drawn as HTML on the page

let shelfQueued = false;
function requestShelf() {
  // Inside the headset the page is not seen, and the panel looks after itself every frame.
  if (shelfQueued || vr.mode === "immersive") return;
  shelfQueued = true;
  requestAnimationFrame(() => {
    shelfQueued = false;
    renderShelfPage();
  });
}

function button(act, icon, cls = "sh-btn", title = "") {
  return `<button class="${cls}" data-act="${act}"${title ? ` title="${title}"` : ""}>${iconSvg(icon)}</button>`;
}

function buildShelfPage() {
  el("close").innerHTML = iconSvg("back");
  el("shelf-bar").innerHTML = `
    <div class="sh-top">
      <span class="sh-title" id="sh-title"></span>
      <span class="sh-sub" id="sh-sub"></span>
      <div class="sh-right">
        <div class="sh-stars" id="sh-stars">${[1, 2, 3, 4, 5].map((n) =>
          `<button class="sh-star" data-act="rate:${n}" title="${n} star${n > 1 ? "s" : ""}"></button>`).join("")}</div>
        ${button("fav", "heart", "sh-mini", "Favorite")}
        <button class="sh-mini" data-act="delete" id="sh-delete" title="Delete - press twice">${iconSvg("trash")}</button>
        ${button("full", "expand", "sh-mini", "Full screen")}
        <button class="sh-chip go" data-act="vr" id="sh-vr" hidden title="Watch in the headset">${iconSvg("goggles")}VR</button>
        <button class="sh-chip mark" data-act="mark" id="sh-mark" title="Mark where a scene starts, then where it ends"></button>
        <button class="sh-chip lens" data-act="lensChip" id="sh-lens" title="How this film is shaped"></button>
        <span class="sh-chip stereo" id="sh-stereo"></span>
        <span class="sh-time" id="sh-time"></span>
      </div>
    </div>
    <div class="sh-scrub" id="sh-scrub">
      <div class="sh-track" id="sh-track">
        <div class="sh-buf" id="sh-buf"></div>
        <div class="sh-prog" id="sh-prog"></div>
        <div id="sh-ticks"></div>
        <div id="sh-scenes"></div>
        <div class="sh-knob" id="sh-knob"></div>
        <div class="sh-hoverline" id="sh-hoverline" hidden></div>
        <div class="sh-bubble" id="sh-bubble" hidden>
          <div class="sh-thumb" id="sh-thumb"></div>
          <div class="sh-bubble-row"><span class="sh-bubble-time" id="sh-htime"></span>
            <span class="sh-bubble-chapter" id="sh-hchapter"></span></div>
        </div>
      </div>
    </div>
    <div class="sh-row">
      <div class="sh-left">
        <button class="sh-vol" data-act="volume" id="sh-vol" title="Volume">
          <span id="sh-vol-icon"></span><span class="sh-volbar"><span class="sh-volfill" id="sh-volfill"></span></span>
        </button>
      </div>
      <div class="sh-center">
        ${button("prev", "prev", "sh-btn", "Previous")}
        <button class="sh-btn sh-skip" data-act="back10" title="Back 10s">${iconSvg("ccw")}<b>10</b></button>
        <button class="sh-btn sh-play" data-act="play" id="sh-play" title="Play / pause"></button>
        <button class="sh-btn sh-skip" data-act="fwd10" title="Forward 10s">${iconSvg("cw")}<b>10</b></button>
        ${button("next", "next", "sh-btn", "Next")}
      </div>
      <div class="sh-tools">
        <button class="sh-btn" data-act="threeD" id="sh-3d" title="3D / 2D">${iconSvg("box")}</button>
        <button class="sh-btn" data-act="passthrough" id="sh-pt" title="Passthrough">${iconSvg("eye")}</button>
        ${button("recenter", "locate", "sh-btn", "Recenter")}
        <button class="sh-btn" data-act="settings" id="sh-settings" title="Settings">${iconSvg("sliders")}</button>
        ${button("collapse", "down", "sh-btn soft", "Collapse")}
      </div>
    </div>`;

  // Everything pressable says what it is with data-act, and this one listener hands it on.
  el("player").addEventListener("click", (event) => {
    const target = event.target.closest("[data-act]");
    if (target && el("player").contains(target)) act(target.dataset.act);
  });
  el("shelf-settings").addEventListener("input", (event) => {
    const slider = event.target.closest("[data-slider]");
    if (!slider) return;
    setSlider(slider.dataset.slider, Number(slider.value) / 1000);
    const said = slider.parentElement.querySelector(".st-slider-head span");
    const model = shelfModel();
    const found = [...(model.tab === "audio" ? [{ key: "volume", valueLabel: `${Math.round(video.volume * 100)}%` }] : []),
                   ...model.screen.sliders].find((s) => s.key === slider.dataset.slider);
    if (said && found) said.textContent = found.valueLabel;   // updated in place; rebuilding would drop the drag
    renderShelfBar(model);
  });

  // The scan bar: pointing shows where, pressing and dragging goes there.
  const scrub = el("sh-scrub");
  const fraction = (event) => {
    const box = el("sh-track").getBoundingClientRect();
    return Math.max(0, Math.min(1, (event.clientX - box.left) / box.width));
  };
  let dragging = false;
  scrub.addEventListener("pointerdown", (event) => {
    dragging = true;
    scrub.setPointerCapture(event.pointerId);
    shelf.scrubHover = fraction(event);
    requestShelf();
  });
  scrub.addEventListener("pointermove", (event) => {
    shelf.scrubHover = fraction(event);
    requestShelf();
  });
  scrub.addEventListener("pointerup", (event) => {
    if (!dragging) return;
    dragging = false;
    act("seek", fraction(event));
  });
  scrub.addEventListener("pointerleave", () => {
    if (dragging) return;
    shelf.scrubHover = null;
    requestShelf();
  });
  scrub.addEventListener("pointercancel", () => { dragging = false; shelf.scrubHover = null; requestShelf(); });
}

let settingsDrawn = "";

function renderShelfPage() {
  if (el("player").hidden) return;
  const m = shelfModel();
  renderShelfBar(m);
  el("shelf-bar").hidden = m.collapsed;
  el("shelf-collapsed").hidden = !m.collapsed;
  if (m.collapsed) {
    el("shelf-collapsed").innerHTML = `
      <button class="cl-play" data-act="play" title="Play / pause">${iconSvg(m.playing ? "pause" : "play")}</button>
      <div class="cl-info"><div class="cl-title">${escapeHtml(m.title)} <span>· ${m.cur}</span></div>
        <div class="cl-bar"><div class="cl-fill" style="width:${(m.progress * 100).toFixed(2)}%"></div></div></div>
      <button class="cl-expand" data-act="expand" title="Show the controls">${iconSvg("up")}</button>`;
  }
  const settings = el("shelf-settings");
  settings.hidden = !m.settings || m.collapsed;
  if (!settings.hidden) {
    // Rebuilt only when something in it changed - never mid-drag, or the slider under the finger
    // would be replaced by a new one and the drag would stop.
    const signature = JSON.stringify([m.tab, m.projections, m.stereos, m.screen.quality, m.envs,
                                      m.passthroughOptions, m.muted, m.screen.resetLabel, m.screen.sliders.map((s) => s.key),
                                      m.scenes.map((s) => [s.id, s.label, s.times])]);
    if (signature !== settingsDrawn || !settings.firstChild) {
      settingsDrawn = signature;
      settings.innerHTML = settingsHtml(m);
    }
  } else {
    settingsDrawn = "";
  }
  const paused = el("shelf-paused");
  paused.hidden = m.playing || m.settings || !state.current || vr.mode === "immersive";
  if (!paused.hidden) {
    const line = [m.subtitle, `${m.remain} left`].filter(Boolean).join(" · ");
    paused.innerHTML = `<div class="ps-text"><span class="ps-kicker">Paused</span>
      <span class="ps-title">${escapeHtml(m.title)}</span><span class="ps-line">${escapeHtml(line)}</span></div>`;
  }
}

function renderShelfBar(m) {
  el("sh-title").textContent = m.title;
  el("sh-sub").textContent = m.subtitle;
  el("sh-lens").textContent = m.lens;
  el("sh-mark").textContent = m.marking ? `End scene · ${m.marking.at}` : "Mark scene";
  el("sh-mark").classList.toggle("marking", Boolean(m.marking));
  el("sh-stereo").textContent = m.stereo;
  el("sh-time").textContent = `${m.cur} / ${m.total}`;
  document.querySelectorAll("#sh-stars .sh-star").forEach((star, i) => {
    const on = i < m.rating;
    star.classList.toggle("on", on);
    star.innerHTML = iconSvg(on ? "starFull" : "star");
  });
  const fav = document.querySelector('#shelf-bar [data-act="fav"]');
  fav.classList.toggle("fav-on", m.fav);
  fav.innerHTML = iconSvg(m.fav ? "heartFull" : "heart");
  el("sh-delete").classList.toggle("armed", m.armed);
  el("sh-prog").style.width = `${(m.progress * 100).toFixed(3)}%`;
  el("sh-buf").style.width = `${(m.buffered * 100).toFixed(2)}%`;
  el("sh-knob").style.left = `${(m.progress * 100).toFixed(3)}%`;
  const band = (from, to, cls) =>
    `<div class="${cls}" style="left:${(from * 100).toFixed(2)}%;width:${(Math.max(0.002, to - from) * 100).toFixed(2)}%"></div>`;
  const bands = m.scenes.map((s) => band(s.from, s.to, "sh-scene")).join("")
              + (m.marking ? band(m.marking.from, m.marking.to, "sh-scene marking") : "");
  if (el("sh-scenes").innerHTML !== bands) el("sh-scenes").innerHTML = bands;
  const ticks = m.ticks.map((t) => `<div class="sh-tick" style="left:${(t * 100).toFixed(2)}%"></div>`).join("");
  if (el("sh-ticks").innerHTML !== ticks) el("sh-ticks").innerHTML = ticks;
  const pointing = m.scrubHover != null;
  el("sh-hoverline").hidden = el("sh-bubble").hidden = !pointing;
  if (pointing) {
    el("sh-hoverline").style.left = `${(m.scrubHover * 100).toFixed(2)}%`;
    el("sh-bubble").style.left = `${(Math.max(0.1, Math.min(0.9, m.scrubHover)) * 100).toFixed(2)}%`;
    el("sh-htime").textContent = m.hoverTime;
    el("sh-hchapter").textContent = m.hoverChapter;
    const strip = shelf.strip, thumb = el("sh-thumb");
    if (strip) {
      const n = Math.min(strip.count - 1, Math.floor(m.scrubHover * strip.count));
      const col = n % strip.cols, row = Math.floor(n / strip.cols), rows = Math.ceil(strip.count / strip.cols);
      thumb.style.backgroundImage = `url("${strip.url}")`;
      thumb.style.backgroundSize = `${strip.cols * 220}px ${rows * 122}px`;
      thumb.style.backgroundPosition = `-${col * 220}px -${row * 122}px`;
    } else {
      thumb.style.backgroundImage = "";
    }
    thumb.textContent = strip ? "" : m.stillsNote;
  }
  el("sh-vol-icon").innerHTML = iconSvg(m.muted ? "mute" : "volume");
  el("sh-volfill").style.width = `${m.muted ? 0 : Math.round(m.volume * 100)}%`;
  el("sh-play").innerHTML = iconSvg(m.playing ? "pause" : "play");
  el("sh-3d").className = `sh-btn${m.threeD && m.threeDAvailable ? " on" : ""}${m.threeDAvailable ? "" : " faint"}`;
  el("sh-pt").className = `sh-btn${m.passthrough ? " on" : ""}`;
  el("sh-settings").className = `sh-btn${m.settings ? " active" : ""}`;
}

function escapeHtml(text) {
  return String(text || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const SETTINGS_TABS = [["projection", "Projection"], ["screen", "Screen"], ["audio", "Audio"], ["env", "Environment"],
                       ["scenes", "Scenes"]];

function glyphStyle(p) {
  const colour = p.on ? "#c67139" : "#a19786";
  if (!p.deg) return `background:linear-gradient(${colour},${colour}) center/28px 17px no-repeat,#dcd3c4`;
  return `background:conic-gradient(from ${-p.deg / 2}deg,${colour} 0 ${p.deg}deg,#dcd3c4 0)`;
}

function sliderHtml(s) {
  return `<label class="st-slider"><span class="st-slider-head">${escapeHtml(s.label)}<span>${escapeHtml(s.valueLabel)}</span></span>
    <input class="st-range" type="range" min="0" max="1000" value="${Math.round(s.value * 1000)}" data-slider="${s.key}"></label>`;
}

function pillsHtml(options, prefix) {
  return `<div class="st-pills">${options.map((o) =>
    `<button class="st-pill${o.on ? " on" : ""}" data-act="${prefix}:${escapeHtml(o.key)}">${escapeHtml(o.label)}</button>`).join("")}</div>`;
}

function settingsHtml(m) {
  const tabs = SETTINGS_TABS.map(([key, label]) =>
    `<button class="st-tab${m.tab === key ? " on" : ""}" data-act="tab:${key}">${label}</button>`).join("");
  let body = "";
  if (m.tab === "projection") {
    body = `<div class="st-grid">${m.projections.map((p) => `
      <button class="st-card${p.on ? " on" : ""}" data-act="proj:${p.key}">
        <span class="st-glyph${p.deg ? "" : " flat"}" style="${glyphStyle(p)}"></span>
        <span class="st-card-text"><span class="st-card-label">${p.label}</span><span class="st-card-sub">${p.sub}</span></span>
      </button>`).join("")}</div>
      <div class="st-row"><span class="st-row-label">Stereo format</span><div class="st-seg">${m.stereos.map((s) =>
        `<button class="${s.on ? "on" : ""}" data-act="stereo:${s.key}">${s.label}</button>`).join("")}</div></div>`;
  } else if (m.tab === "screen") {
    body = `<div style="display:flex;flex-direction:column;gap:18px;padding-top:4px">
      ${m.screen.sliders.map(sliderHtml).join("")}
      <div class="st-actions">
        <button class="st-primary" data-act="btn:recenter">${iconSvg("locate")}Recenter view</button>
        <button class="st-outline" data-act="btn:resetScreen">${m.screen.resetLabel}</button>
      </div>
      <span class="st-subhead">Playback</span>${pillsHtml(m.screen.quality, "quality")}
      ${m.screen.hint ? `<span class="st-note">${m.screen.hint}</span>` : ""}</div>`;
  } else if (m.tab === "scenes") {
    body = m.scenes.length ? `<div class="st-scenes">${m.scenes.map((s) => `
      <div class="st-scene"><span class="st-scene-text"><span class="st-scene-name">${escapeHtml(s.label)}</span>
        <span class="st-scene-time">${s.times}</span></span>
        <button class="st-pill" data-act="scene-go:${s.id}">Play</button>
        <button class="st-pill" data-act="scene-start:${s.id}">Start here</button>
        <button class="st-pill" data-act="scene-end:${s.id}">End here</button>
        <button class="st-pill" data-act="scene-del:${s.id}" title="Remove this scene">${iconSvg("x")}</button></div>`).join("")}</div>`
      : `<span class="st-note">No scenes marked in this film yet.</span>`;
    body += `<span class="st-note">Press <b>Mark scene</b> where one starts and again where it ends.
      Scenes are made into passthrough copies on the PC, in Make passthrough copies, where they can be
      put in order.</span>`;
  } else if (m.tab === "audio") {
    body = `${sliderHtml({ key: "volume", label: "Volume", valueLabel: `${Math.round(m.volume * 100)}%`, value: m.volume })}
      ${pillsHtml([{ key: "", label: m.muted ? "Sound off - press to turn it on" : "Mute", on: m.muted }], "mute")}`;
  } else {
    body = `<div class="st-grid">${m.envs.map((o) => `
      <button class="st-env${o.on ? " on" : ""}" data-act="env:${o.key}">
        <span class="st-swatch" style="background:${o.swatch};color:${o.swatchFg}">${o.hint}</span>
        <span class="st-env-label">${o.label}</span></button>`).join("")}</div>
      ${m.passthroughOptions ? `<span class="st-subhead">Show your room using</span>${pillsHtml(m.passthroughOptions.modes, "see")}
        ${m.passthroughOptions.masks.length ? `<span class="st-subhead">Cut-out</span>${pillsHtml(m.passthroughOptions.masks, "mask")}` : ""}` : ""}
      <span class="st-note">Worlds are drawn around flat films in the headset. Passthrough needs the headset's own room view.</span>`;
  }
  const label = (SETTINGS_TABS.find(([key]) => key === m.tab) || SETTINGS_TABS[0])[1];
  return `<div class="st-side"><div class="st-heading">Settings</div>${tabs}</div>
    <div class="st-main"><div class="st-head"><span class="st-label">${label}</span>
      <button class="st-close" data-act="close" title="Close">${iconSvg("x")}</button></div>${body}</div>`;
}

for (const event of ["play", "pause", "timeupdate", "progress", "volumechange", "durationchange"]) {
  video.addEventListener(event, requestShelf);
}
video.onerror = () => {
  if (!state.converting) {
    state.forced = true;
    toast("This file needs converting for this device");
    loadSource(state.offset + (video.currentTime || 0));
  } else {
    toast("Could not play this video");
  }
};

// Which eye a VR film shows while it is a window on the page. The E key; the page has no room
// for a button that only matters before going into the headset.
function toggleEye() {
  state.eye = state.eye ? 0 : 1;
  if (vr.mode === "flat" && state.current) buildScene(state.current, false);
  toast(state.eye ? "Right eye" : "Left eye");
}

// A VR video goes on the sphere, anything else stays a plain video in the page.
function showSurface(item) {
  if (item.is_vr) startFlat(item);
  else stopFlat();
}

// ---------------------------------------------------------------- the VR view
// A VR video is not a flat picture: it is one or two fisheye or equirectangular eyes wrapped
// around a sphere, so cropping a corner of it in the page can only ever look wrong. It is drawn
// on a sphere instead - as a window you can drag while the page is on a screen, and as the real
// thing once the headset takes over. Both use the same scene, so the VR button only has to swap
// the camera and split the eyes.
//
// Inside the headset the picture can be turned, tilted and zoomed, because a scene shot for
// someone sitting up is wrong for someone lying down, and the camera is rarely exactly where the
// viewer's head is. Those adjustments are kept per video.

// One shader for every projection. Zoom has to happen here rather than by moving the sphere: a
// sphere centred on the viewer looks the same size however big it is, so magnifying means
// changing which part of the picture each direction shows.
const SPHERE_SHADER = {
  vertex: `varying vec2 vUv;
    varying vec3 vDir;
    void main() {
      vUv = uv;
      vDir = position;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`,
  fragment: `uniform sampler2D map;
    uniform float zoom;
    uniform vec4 eyeRect;
    uniform int fisheye;
    uniform float lensFov;
    uniform int seeThrough;     // 0 none, 1 the packed mask, 2 a backdrop colour, 3 colour only
    uniform sampler2D maskMap;  // a mask kept beside the film, played alongside it
    uniform int separate;       // 1 when the cut-out comes from maskMap rather than the corners
    uniform vec2 maskStep;      // one pixel of the mask, in picture coordinates
    uniform vec3 keyColour;
    uniform float keyTolerance;
    uniform float keySoftness;
    varying vec2 vUv;
    varying vec3 vDir;

    // An alpha-packed scene carries its cut-out mask as red, shrunk to 40% of the frame and cut
    // into six pieces that fill the black gaps around the fisheye circles: the mask's left half
    // goes to the middle top and bottom, and its right half is quartered into the corners. These
    // fractions are the packing DeoVR published, read back the other way round.
    vec2 maskPlace(vec2 image) {
      float mx = image.x, my = image.y;
      float x, y;
      if (mx < 0.5) {
        x = 0.4 + mx * 0.4;
        y = my < 0.5 ? 0.8 + my * 0.4 : (my - 0.5) * 0.4;
      } else if (mx < 0.75) {
        x = 0.9 + (mx - 0.5) * 0.4;
        y = my < 0.5 ? 0.8 + my * 0.4 : (my - 0.5) * 0.4;
      } else {
        x = (mx - 0.75) * 0.4;
        y = my < 0.5 ? 0.8 + my * 0.4 : (my - 0.5) * 0.4;
      }
      return vec2(x, 1.0 - y);   // back into texture coordinates, which count up from the bottom
    }

    // The cut-out's edge, read as the average of five points rather than one.
    //
    // The mask is a fraction of the film's size - a 2160-wide mask over a 7200-wide picture -
    // so each of its pixels covers three or four of the picture's, and the edge between kept
    // and cut arrives as a staircase. Cutting at a hard threshold turned that staircase into
    // spikes radiating off the performer. Averaging across one mask pixel, then fading over a
    // wider band, gives an edge that is soft in the way the matting model meant it to be.
    float cutout(sampler2D from, vec2 at, vec2 step) {
      float middle = texture2D(from, at).r;
      float around = texture2D(from, at + vec2(step.x, step.y)).r
                   + texture2D(from, at + vec2(-step.x, step.y)).r
                   + texture2D(from, at + vec2(step.x, -step.y)).r
                   + texture2D(from, at + vec2(-step.x, -step.y)).r;
      return (middle * 2.0 + around) / 6.0;
    }

    float saturationOf(vec3 c) {
      float high = max(c.r, max(c.g, c.b));
      float low = min(c.r, min(c.g, c.b));
      return high <= 0.0001 ? 0.0 : (high - low) / high;
    }

    // three.js leaves a video's colours sRGB-encoded in the texture and decodes them only
    // inside its own materials. This shader is not one of those, so it read them still
    // encoded and then had them encoded again on the way out - lifted blacks, flat contrast,
    // the whole film washed out beside the same file in DeoVR. Its own formula, done here.
    vec3 decoded(vec3 c) {
      return mix(c * 0.0773993808, pow(c * 0.9478672986 + vec3(0.0521327014), vec3(2.4)),
                 vec3(greaterThan(c, vec3(0.04045))));
    }

    void main() {
      vec2 uv;
      if (fisheye == 1) {
        vec3 d = normalize(vDir);
        float theta = acos(clamp(-d.z, -1.0, 1.0)) / zoom;
        float r = theta / radians(lensFov * 0.5);
        if (r > 1.0) { gl_FragColor = vec4(0.0, 0.0, 0.0, seeThrough == 0 ? 1.0 : 0.0); return; }
        // +x is to your right, and the right of a fisheye circle is what was to the camera's
        // right. This read -x for a long time: each eye mirrored, the gap between the eyes
        // reversed, and a film on its correct lens seen double.
        float phi = atan(d.y, d.x);
        uv = eyeRect.xy + vec2(0.5 + 0.5 * r * cos(phi), 0.5 + 0.5 * r * sin(phi)) * eyeRect.zw;
      } else {
        vec2 centre = eyeRect.xy + eyeRect.zw * 0.5;
        uv = centre + (vUv - centre) / zoom;
        if (uv.x < eyeRect.x || uv.y < eyeRect.y || uv.x > eyeRect.x + eyeRect.z || uv.y > eyeRect.y + eyeRect.w) {
          gl_FragColor = vec4(0.0, 0.0, 0.0, seeThrough == 0 ? 1.0 : 0.0);
          return;
        }
      }
      vec4 colour = texture2D(map, uv);
      if (seeThrough == 1) {
        // A separate mask has the film's own layout, just smaller, so the same spot is read
        // straight off it. A packed one is hidden in the corners and has to be looked up.
        vec2 image = vec2(uv.x, 1.0 - uv.y);          // where this pixel sits in the frame
        // A packed mask is quartered into the frame's corners, so its neighbours are only
        // neighbours within one of those pieces - and 40% of an eye across, it is read with
        // the packing's own step.
        float mask = separate == 1 ? cutout(maskMap, uv, maskStep * 0.5)
                                   : cutout(map, maskPlace(image), maskStep * 0.2);
        colour.a = smoothstep(0.22, 0.68, mask);
      } else if (seeThrough == 2) {
        float distance = length(colour.rgb - keyColour);
        colour.a = smoothstep(keyTolerance, keyTolerance + max(keySoftness, 0.001), distance);
      } else if (seeThrough == 3) {
        colour.a = smoothstep(keyTolerance, keyTolerance + max(keySoftness, 0.001), saturationOf(colour.rgb));
      }
      if (colour.a < 0.02) discard;
      // Only now: the mask and the backdrop key above were tuned on the encoded values.
      colour.rgb = decoded(colour.rgb);
      gl_FragColor = colour;
      #include <colorspace_fragment>
    }`,
};

const DEG = Math.PI / 180;
const ZOOM_MIN = 0.5, ZOOM_MAX = 3.0;
const TILT_LIMIT = 85 * DEG, ROLL_LIMIT = 45 * DEG;

// Passthrough: the headset's cameras behind the video, with the background taken out. Scenes
// shot for this carry a cut-out mask packed into the frame; where one is missing, a backdrop
// colour taken from the picture, or "keep whatever still has colour", stand in for it.
const SEE_OFF = 0, SEE_MASK = 1, SEE_BACKDROP = 2, SEE_COLOUR = 3;

const vr = {
  see: { mode: SEE_OFF, colour: [0.35, 0.4, 0.5], tolerance: 0.22, softness: 0.1 },
  inAR: false,  // whether this session is really showing the room
  renderer: null, scene: null, camera: null, texture: null, rig: null, meshes: [], controllers: [],
  mode: "off",  // off, flat (a window in the page) or immersive (the headset)
  item: null, yaw: 0, pitch: 0, fov: 90,
  view: { yaw: 0, pitch: 0, roll: 0, zoom: 1 },
  panel: null, canvas: null, panelTexture: null, hover: null, painted: 0, failed: false,
  scrub: null,       // the trigger is held on the progress bar: { controller, fraction }
  stickSeek: null,   // the right stick is pushed sideways: { time }
  grab: null,        // a grip is held to drag the picture: { controller, yaw, pitch, start }
  pressed: {},       // last state of each controller button, so a press acts once
  lastFrame: 0,
  group: null,     // which set of extra panel buttons is open: "view", "more" or none
  maskName: "",    // the cut-out chosen for this film, "off", or "" when it has none
};

const stage = () => el("vr-stage");

// ---------------------------------------------------------------- the view, kept per video

const viewKey = (item) => `blueshelf-view-${item.id}`;
let saveTimer = 0;

function loadView(item) {
  vr.view = { yaw: 0, pitch: 0, roll: 0, zoom: 1 };
  try {
    const saved = JSON.parse(remember.get(viewKey(item)) || "null");
    if (saved && typeof saved === "object") Object.assign(vr.view, saved);
  } catch (error) { /* a damaged value just means the default view */ }
}

function saveView() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    if (vr.item) remember.set(viewKey(vr.item), JSON.stringify(vr.view));
  }, 400);
}

function applyView() {
  const view = vr.view;
  view.zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, view.zoom));
  view.pitch = Math.max(-TILT_LIMIT, Math.min(TILT_LIMIT, view.pitch));
  view.roll = Math.max(-ROLL_LIMIT, Math.min(ROLL_LIMIT, view.roll));
  view.yaw = Math.atan2(Math.sin(view.yaw), Math.cos(view.yaw));
  if (!vr.rig) return;
  vr.rig.rotation.set(view.pitch, view.yaw, view.roll, "YXZ");
  for (const mesh of vr.meshes) {
    if (mesh.material.uniforms) mesh.material.uniforms.zoom.value = mesh.userData.screen ? 1 : view.zoom;
  }
  applySeeThrough();
}

function applySeeThrough() {
  for (const mesh of vr.meshes) {
    const uniforms = mesh.material.uniforms;
    if (!uniforms || !uniforms.seeThrough) continue;
    uniforms.seeThrough.value = vr.see.mode;
    // The mask often arrives after the scene is built, so it is attached here too.
    uniforms.maskMap.value = vr.maskTexture || blankMask();
    uniforms.separate.value = vr.maskTexture ? 1 : 0;
    uniforms.maskStep.value.copy(maskStep());
    uniforms.keyColour.value.set(...vr.see.colour);
    uniforms.keyTolerance.value = vr.see.tolerance;
    uniforms.keySoftness.value = vr.see.softness;
  }
}

// ---------------------------------------------------------------- a mask kept beside the film
// Rather than the cut-out being written into a new copy of the film - hours of re-encoding
// and tens of gigabytes - it can sit next to the original as its own small video, and the
// two are laid together here as they play. Only this player understands it: DeoVR and the
// headset's own apps still need a packed copy.

// One pixel of the cut-out, as a fraction of the picture. A separate mask is measured from
// the video itself once it is playing; until then, and for a packed one, the size the packing
// uses is assumed.
function maskStep() {
  const mask = vr.maskVideo;
  const wide = (mask && mask.videoWidth) || 2160;
  const tall = (mask && mask.videoHeight) || 1080;
  return new THREE.Vector2(1 / wide, 1 / tall);
}

let blank = null;
function blankMask() {
  if (!blank) {
    blank = new THREE.DataTexture(new Uint8Array([255, 255, 255, 255]), 1, 1);
    blank.needsUpdate = true;
  }
  return blank;
}

function detachMask() {
  const mask = vr.maskVideo;
  if (mask) {
    mask.pause();
    mask.removeAttribute("src");
    mask.load();
    mask.remove();
  }
  if (vr.maskTexture) vr.maskTexture.dispose();
  vr.maskVideo = vr.maskTexture = null;
  vr.maskShownToken = null;       // stops the old mask's frame callbacks
  vr.maskShown = null;
  vr.maskState = "";
  applySeeThrough();
}

// A film can have several cut-outs beside it - one per matting model, say - and which looks
// best is only clear with the headset on, so they are stepped through while watching, and
// the one picked is remembered for that film. "off" is remembered too.
const maskKey = (item) => `blueshelf-mask-${item.id}`;

function chosenMask(item) {
  const masks = item.masks || [];
  const saved = remember.get(maskKey(item));
  if (saved === "off" && masks.length) return "off";
  return masks.includes(saved) ? saved : (masks[0] || "");
}

function setMask(name) {
  const item = state.current;
  if (!item || !(item.masks || []).length) return;
  remember.set(maskKey(item), name);
  if (name === "off") {
    detachMask();
    vr.maskName = "off";
    item.has_mask = false;
    applySeeThrough();
    toast("Using the cut-out packed into the film, if it has one", 2000);
  } else {
    playMask(item, name);
    toast(`Cut-out: ${name}`, 1500);
  }
  requestShelf();
}

async function attachMask(item) {
  detachMask();
  vr.maskName = "";
  let detail = null;
  try {
    // The library listing never says whether a mask exists - finding out means asking the
    // disk, which is too slow to do for every video - so the one being opened is asked about.
    detail = await (await api(`/api/video/${item.id}`)).json();
  } catch (error) { return; }
  if (!detail || state.current !== item) return;
  item.masks = detail.masks || [];
  shelf.chapters = detail.chapters || [];
  shelf.scenes = detail.scenes || [];
  const name = chosenMask(item);
  vr.maskName = name;
  requestShelf();
  if (name && name !== "off") playMask(item, name);
}

function playMask(item, name) {
  detachMask();
  vr.maskName = name;
  const mask = document.createElement("video");
  mask.muted = true;
  mask.playsInline = true;
  mask.preload = "auto";
  // Part of the page, though invisible, like the film itself: a headset's browser will not
  // decode a video that is only held in memory, and a mask that never decodes reads as
  // "nothing here" and cuts the whole picture away.
  mask.className = "mask-source";
  document.body.appendChild(mask);
  const query = [`m=${encodeURIComponent(name)}`];
  if (KEY) query.push(`k=${encodeURIComponent(KEY)}`);
  mask.src = `${at(`/mask/${item.id}`)}?${query.join("&")}`;
  vr.maskVideo = mask;
  vr.maskState = "loading";
  requestShelf();

  let settled = false;
  const giveUp = (why) => {
    if (settled || vr.maskVideo !== mask) return;
    settled = true;
    detachMask();
    vr.maskState = "failed";
    requestShelf();
    toast(`The cut-out beside this film would not play here (${why}), so it is shown whole`, 5000);
  };
  const timer = setTimeout(() => giveUp("it never started"), 15000);
  mask.addEventListener("error", () => { clearTimeout(timer); giveUp("it could not be read"); });
  mask.addEventListener("loadeddata", () => {
    if (settled || vr.maskVideo !== mask) return;
    settled = true;
    clearTimeout(timer);
    // Only now is anything cut out. Switching before the mask has a frame would read the
    // empty texture as "nothing here" and black out the whole picture - which is how a mask
    // that fails to play on a headset showed up as a black screen with no explanation.
    vr.maskTexture = new THREE.VideoTexture(mask);
    followFrames(mask, "maskShown");
    vr.maskState = "on";
    item.has_mask = true;
    vr.see.mode = SEE_MASK;
    applySeeThrough();
    requestShelf();
    toast("Playing with the cut-out kept beside this film", 2000);
  }, { once: true });
  // Some headsets ignore preload until asked to play. It is muted, so this is allowed, and
  // syncMask pauses it again straight away if the film is not playing.
  mask.play().catch(() => {});
}

// The time of the frame actually on screen, from the browser's own frame callbacks where it
// has them. Plain currentTime is only updated a few times a second on some headsets, and
// steering the mask by a clock that jumps made it lurch about.
function followFrames(element, key) {
  vr[key] = null;
  if (!element.requestVideoFrameCallback) return;
  const token = {};
  vr[`${key}Token`] = token;
  const tick = (_now, meta) => {
    if (vr[`${key}Token`] !== token) return;
    vr[key] = meta.mediaTime;
    element.requestVideoFrameCallback(tick);
  };
  element.requestVideoFrameCallback(tick);
}

function setRate(element, want) {
  if (Math.abs(element.playbackRate - want) > 0.001) element.playbackRate = want;
}

// Two videos drift apart on their own, and a mask even a few frames behind leaves a ghost
// trailing the performer. This used to steer the mask continuously - a slightly different
// speed every frame, up to ninety times a second in a headset - and headset browsers stall
// their decoder when the speed keeps changing, which is itself a stutter. Now it looks a
// few times a second, leaves anything under two frames alone, nudges with one small fixed
// step and changes speed only when the direction must change. Only a real jump is fixed by
// seeking, since seeking stalls it.
function syncMask() {
  const mask = vr.maskVideo;
  if (!mask || mask.readyState < 1) return;
  const now = performance.now();
  if (now - (vr.maskCheckedAt || 0) < 200) return;
  vr.maskCheckedAt = now;
  const rate = video.playbackRate || 1;
  const playing = !video.paused && !video.seeking;
  const filmAt = state.offset + ((playing && vr.filmShown != null) ? vr.filmShown
                                                                    : (video.currentTime || 0));
  if (!playing) {
    if (!mask.paused) mask.pause();
    if (Math.abs(filmAt - mask.currentTime) > 0.02 && !mask.seeking) mask.currentTime = filmAt;
    return;
  }
  if (mask.paused) {
    mask.currentTime = filmAt;
    mask.play().catch(() => {});
    return;
  }
  if (mask.seeking) return;
  const maskAt = vr.maskShown != null ? vr.maskShown : mask.currentTime;
  const drift = filmAt - maskAt;
  if (Math.abs(drift) > 0.4) {
    mask.currentTime = filmAt;
    setRate(mask, rate);
    return;
  }
  setRate(mask, Math.abs(drift) < 0.035 ? rate : rate * (drift > 0 ? 1.05 : 0.95));
}

// A backdrop colour taken from the picture, rather than assuming green: the corners of a frame
// are backdrop in nearly every scene shot this way.
function sampleBackdrop() {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 32;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(video, 0, 0, 32, 32);
    const data = ctx.getImageData(0, 0, 32, 32).data;
    const spots = [[8, 3], [23, 3], [8, 28], [23, 28], [16, 3]];
    let red = 0, green = 0, blue = 0;
    for (const [x, y] of spots) {
      const at = (y * 32 + x) * 4;
      red += data[at]; green += data[at + 1]; blue += data[at + 2];
    }
    const total = spots.length * 255;
    vr.see.colour = [red / total, green / total, blue / total];
  } catch (error) { /* an unreadable frame leaves the last colour alone */ }
}

function adjustView(change) {
  vr.view.yaw += change.yaw || 0;
  vr.view.pitch += change.pitch || 0;
  vr.view.roll += change.roll || 0;
  if (change.zoom) vr.view.zoom *= change.zoom;
  applyView();
  saveView();
}

function resetView() {
  vr.view = { yaw: 0, pitch: 0, roll: 0, zoom: 1 };
  applyView();
  saveView();
  toast("View reset", 900);
}

// ---------------------------------------------------------------- building the sphere

function eyeGeometry(item, eye) {
  // A sphere seen from the inside. For equirect the UVs are simply remapped for the eye; fisheye
  // works the mapping out per pixel from the direction instead.
  const span = item.vr_proj === PROJ_360 ? Math.PI * 2 : Math.PI;
  const geometry = new THREE.SphereGeometry(500, 60, 40, -span / 2 - Math.PI / 2, span);
  geometry.scale(-1, 1, 1);
  if (item.vr_proj !== PROJ_FISHEYE) {
    const uv = geometry.attributes.uv;
    for (let i = 0; i < uv.count; i++) {
      let u = uv.getX(i), v = uv.getY(i);
      if (item.vr_layout === LAYOUT_SBS) u = (u + eye) * 0.5;
      else if (item.vr_layout === LAYOUT_TB) v = (v + eye) * 0.5;
      uv.setXY(i, u, v);
    }
    uv.needsUpdate = true;
  }
  return geometry;
}

function eyeRect(item, eye) {
  if (item.vr_layout === LAYOUT_SBS) return new THREE.Vector4(eye * 0.5, 0, 0.5, 1);
  if (item.vr_layout === LAYOUT_TB) return new THREE.Vector4(0, eye * 0.5, 1, 0.5);
  return new THREE.Vector4(0, 0, 1, 1);
}

function eyeMaterial(item, eye) {
  return new THREE.ShaderMaterial({
    uniforms: {
      map: { value: vr.texture },
      zoom: { value: vr.view.zoom },
      eyeRect: { value: eyeRect(item, eye) },
      fisheye: { value: item.vr_proj === PROJ_FISHEYE ? 1 : 0 },
      lensFov: { value: item.vr_lens_fov || 190 },
      seeThrough: { value: vr.see.mode },
      maskMap: { value: vr.maskTexture || blankMask() },
      separate: { value: vr.maskTexture ? 1 : 0 },
      maskStep: { value: maskStep() },
      keyColour: { value: new THREE.Vector3(...vr.see.colour) },
      keyTolerance: { value: vr.see.tolerance },
      keySoftness: { value: vr.see.softness },
    },
    vertexShader: SPHERE_SHADER.vertex,
    fragmentShader: SPHERE_SHADER.fragment,
    side: THREE.FrontSide,
    transparent: true,
    depthFunc: THREE.AlwaysDepth,
  });
}

function eyeMesh(item, eye) {
  return new THREE.Mesh(eyeGeometry(item, eye), eyeMaterial(item, eye));
}

function ensureRenderer() {
  if (vr.renderer || vr.failed) return !vr.failed;
  try {
    const renderer = new THREE.WebGLRenderer({ antialias: false });
    renderer.setPixelRatio(1);
    renderer.xr.enabled = true;
    stage().appendChild(renderer.domElement);
    vr.renderer = renderer;
    vr.scene = new THREE.Scene();
    vr.camera = new THREE.PerspectiveCamera(vr.fov, 1, 0.1, 1000);
    vr.camera.rotation.order = "YXZ";
    // Everything the viewer adjusts hangs off this, at roughly head height, so turning and
    // tilting pivot around the viewer rather than around the floor.
    vr.rig = new THREE.Group();
    vr.rig.position.set(0, 1.5, 0);
    vr.scene.add(vr.rig);
    vr.texture = new THREE.VideoTexture(video);
    vr.texture.colorSpace = THREE.SRGBColorSpace;
    window.addEventListener("resize", fitStage);
  } catch (error) {
    vr.failed = true;
    toast("This browser cannot draw VR videos");
    return false;
  }
  return true;
}

function buildScene(item, stereo) {
  vr.stereo = stereo;      // so a lens change can rebuild exactly as it was
  for (const mesh of vr.meshes) {
    vr.rig.remove(mesh);
    mesh.geometry.dispose();
    mesh.material.dispose();
  }
  if (vr.item !== item) loadView(item);
  vr.item = item;
  const both = stereo && item.vr_layout !== LAYOUT_MONO && shelf.threeD;
  if (!item.is_vr) {
    // A flat film still deserves a screen in front of you rather than a sphere around you -
    // and a 3D one, each eye its own half.
    vr.meshes = both ? [screenMesh(item, 0), screenMesh(item, 1)] : [screenMesh(item, 0)];
    if (both) {
      vr.meshes[0].layers.set(1);
      vr.meshes[1].layers.set(2);
    }
  } else if (both) {
    const left = eyeMesh(item, 0), right = eyeMesh(item, 1);
    left.layers.set(1);   // three.js draws layer 1 to the left eye and layer 2 to the right
    right.layers.set(2);
    vr.meshes = [left, right];
  } else {
    vr.meshes = [eyeMesh(item, item.vr_layout === LAYOUT_MONO ? 0 : state.eye)];
  }
  for (const mesh of vr.meshes) {
    if (vr.meshes.length === 1) mesh.layers.enableAll();
    vr.rig.add(mesh);
  }
  placeScreen();
  applyView();
  applyWorld();
}

// ---------------------------------------------------------------- the window in the page

function fitStage() {
  if (!vr.renderer || vr.mode !== "flat") return;
  const box = stage().getBoundingClientRect();
  if (!box.width || !box.height) return;
  vr.renderer.setSize(box.width, box.height, false);
  vr.camera.aspect = box.width / box.height;
  vr.camera.fov = vr.fov;
  vr.camera.updateProjectionMatrix();
}

function drawFlat() {
  syncMask();
  vr.camera.position.set(0, 1.5, 0);
  vr.camera.rotation.set(vr.pitch, vr.yaw, 0);
  vr.renderer.render(vr.scene, vr.camera);
}

function startFlat(item) {
  if (!ensureRenderer()) return;
  vr.mode = "flat";
  vr.yaw = 0;
  vr.pitch = 0;
  vr.fov = item.vr_proj === PROJ_360 ? 80 : 95;
  buildScene(item, false);
  video.classList.add("as-source");
  stage().hidden = false;
  fitStage();
  vr.renderer.setAnimationLoop(drawFlat);
}

function stopFlat() {
  video.classList.remove("as-source");
  stage().hidden = true;
  if (vr.mode === "flat") {
    vr.renderer.setAnimationLoop(null);
    vr.mode = "off";
  }
}

// Dragging looks around, the wheel or a pinch zooms, exactly like the desktop player.
let drag = null;
stage().addEventListener("pointerdown", (event) => {
  if (vr.mode !== "flat") return;
  drag = { x: event.clientX, y: event.clientY };
  stage().setPointerCapture(event.pointerId);
});
stage().addEventListener("pointermove", (event) => {
  if (!drag || vr.mode !== "flat") return;
  const speed = (vr.fov / 90) * 0.0032;
  vr.yaw += (event.clientX - drag.x) * speed;
  vr.pitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, vr.pitch + (event.clientY - drag.y) * speed));
  drag = { x: event.clientX, y: event.clientY };
});
const endDrag = () => { drag = null; };
stage().addEventListener("pointerup", endDrag);
stage().addEventListener("pointercancel", endDrag);
stage().addEventListener("wheel", (event) => {
  if (vr.mode !== "flat") return;
  event.preventDefault();
  vr.fov = Math.max(30, Math.min(110, vr.fov + Math.sign(event.deltaY) * 5));
  fitStage();
}, { passive: false });

// ---------------------------------------------------------------- the Shelf controls in the headset
// There is no page inside VR, so the same controls are painted onto canvases hanging in front
// of you (shelf-vr.js does the painting). The bar sits where your hands are, settings open
// above it, and a still of the film floats over the scan bar where you point.

const PANEL_WIDTH_M = 1.3;
const PX = PANEL_WIDTH_M / ShelfVR.BAR.w;       // metres per pixel of the design

function buildPanel() {
  if (vr.panel) return;
  const group = new THREE.Group();
  group.visible = false;
  vr.scene.add(group);
  const part = (surface, z = 0) => {
    const texture = new THREE.CanvasTexture(surface.canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    // No mip-maps: they are rebuilt on every repaint, and the canvas is already close to one
    // pixel per pixel of the headset's own at arm's length.
    texture.generateMipmaps = false;
    texture.minFilter = THREE.LinearFilter;
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1),
                                new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false,
                                                              depthTest: false }));
    mesh.layers.enableAll();
    mesh.renderOrder = 20 + z;
    mesh.visible = false;
    group.add(mesh);
    return { surface, texture, mesh, z, drawn: "" };
  };
  vr.ui = {
    bar: part(new ShelfVR.Surface(ShelfVR.BAR.w, ShelfVR.BAR.h, { margin: 40 })),
    pill: part(new ShelfVR.Surface(ShelfVR.PILL.w, ShelfVR.PILL.h, { margin: 40 })),
    settings: part(new ShelfVR.Surface(ShelfVR.SETTINGS.w, ShelfVR.SETTINGS.maxH, { margin: 40, anchor: "bottom" })),
    paused: part(new ShelfVR.Surface(ShelfVR.PAUSED.w, ShelfVR.PAUSED.h, { margin: 20, anchor: "bottom" })),
    bubble: part(new ShelfVR.Surface(ShelfVR.BUBBLE.w, ShelfVR.BUBBLE.h, { margin: 30 }), 1),
  };
  vr.panel = group;
}

// Put the point (x, y) of a part's drawing at (lx, ly) on the panel.
function pin(part, x, y, lx, ly) {
  const S = part.surface;
  const w = (S.width + 2 * S.margin) * PX, h = (S.height + 2 * S.margin) * PX;
  part.mesh.scale.set(w, h, 1);
  part.mesh.position.set(lx - (S.margin + x) * PX + w / 2, ly + (S.margin + S.offsetY + y) * PX - h / 2, part.z * 0.004);
}

// The time the bar is showing: where the pointer is while scrubbing, otherwise where the video is.
function shownTime() {
  const total = totalLength();
  if (vr.scrub && total) return vr.scrub.fraction * total;
  if (vr.stickSeek) return vr.stickSeek.time;
  return state.offset + (video.currentTime || 0);
}

// Painted only when what it shows has changed: a canvas sent to the headset's graphics card
// every frame would cost more than the film.
// Everything the panel shows depends on these; if none has moved since the last frame there is
// nothing to do - which is almost every frame. The play position counts in whole pixels of the
// bar and whole seconds of the clock, not in the fractions that change every frame: comparing
// those repainted the whole bar and sent it to the graphics card ninety times a second.
function panelInputs() {
  const item = state.current || {};
  const total = totalLength();
  const now = shownTime();
  return [shelf.hover, shelf.scrubHover == null ? -1 : Math.round(shelf.scrubHover * 920),
          Math.floor(now), total ? Math.round((now / total) * 920) : 0, video.paused, shelf.settings, shelf.tab,
          shelf.collapsed, Math.round(video.volume * 100), video.muted, shelf.threeD, vr.see.mode, shelf.env,
          vr.maskName, state.armed, shelf.marking, shelf.scenes.map((x) => `${x.id}:${x.start}:${x.end}:${x.label}`).join(),
          item.favorite, item.vr_proj, item.vr_lens_fov, item.vr_layout, item.rating, item.is_vr, vr.mode,
          vr.view.zoom.toFixed(2), vr.view.pitch.toFixed(2), vr.view.yaw.toFixed(2), shelf.screenSize, shelf.screenDistance,
          state.quality, Boolean(shelf.strip), shelf.stillsNote, shelf.chapters.length, Math.round(bufferedShare() * 920),
          (item.masks || []).join()].join("|");
}

function drawShelfVR(force = false) {
  if (!vr.ui || !vr.panel.visible) return;
  const inputs = panelInputs();
  if (!force && inputs === vr.panelInputs) return;
  vr.panelInputs = inputs;
  const m = shelfModel();
  const ui = vr.ui;
  const paint = (part, visible, key, draw) => {
    part.mesh.visible = visible;
    if (!visible || (!force && part.drawn === key)) return;
    part.drawn = key;
    draw();
    part.texture.needsUpdate = true;
  };
  const { projections, envs, screen, passthroughOptions, stereos, progress, buffered, scrubHover, hoverTime,
          hoverChapter, remain, ...barOnly } = m;
  const bar = JSON.stringify([barOnly, Math.round(progress * 920), Math.round(buffered * 920),
                              scrubHover == null ? -1 : Math.round(scrubHover * 920)]);
  paint(ui.bar, !m.collapsed, bar, () => ShelfVR.drawBar(ui.bar.surface, m));
  paint(ui.pill, m.collapsed, JSON.stringify([m.title, m.cur, m.progress, m.playing, m.hover]),
        () => ShelfVR.drawCollapsed(ui.pill.surface, m));
  paint(ui.settings, m.settings && !m.collapsed,
        JSON.stringify([m.tab, projections, stereos, envs, screen, passthroughOptions, m.volume, m.muted, m.hover]),
        () => ShelfVR.drawSettings(ui.settings.surface, m));
  paint(ui.paused, !m.playing && !m.settings && !m.collapsed, JSON.stringify([m.title, m.subtitle, m.remain]),
        () => ShelfVR.drawPaused(ui.paused.surface, m));
  paint(ui.bubble, m.scrubHover != null && !m.collapsed,
        JSON.stringify([m.hoverTime, m.hoverChapter, Boolean(shelf.strip), Math.round((m.scrubHover || 0) * 1000)]),
        () => ShelfVR.drawBubble(ui.bubble.surface, m, shelf.strip));

  const barTop = (ShelfVR.BAR.h / 2) * PX;
  pin(ui.bar, ShelfVR.BAR.w / 2, ShelfVR.BAR.h / 2, 0, 0);
  pin(ui.pill, ShelfVR.PILL.w / 2, ShelfVR.PILL.h / 2, 0, 0);
  pin(ui.settings, ShelfVR.SETTINGS.w / 2, ui.settings.surface.shown, 0, barTop + 16 * PX);
  pin(ui.paused, 0, ShelfVR.PAUSED.h, -(ShelfVR.BAR.w / 2) * PX, barTop + 16 * PX);
  const along = Math.max(0.1, Math.min(0.9, m.scrubHover || 0));
  const span = ShelfVR.BAR.w - 2 * ShelfVR.BAR.padX;
  pin(ui.bubble, ShelfVR.BUBBLE.w / 2, ShelfVR.BUBBLE.h,
      (ShelfVR.BAR.padX + along * span - ShelfVR.BAR.w / 2) * PX, barTop - 54 * PX);
}

function placePanel() {
  const camera = vr.renderer.xr.getCamera();
  const eye = new THREE.Vector3();
  const facing = new THREE.Quaternion();
  camera.getWorldPosition(eye);
  camera.getWorldQuaternion(facing);
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(facing);
  forward.y = 0;
  if (forward.lengthSq() < 1e-4) forward.set(0, 0, -1);
  forward.normalize();
  vr.panel.position.copy(eye).addScaledVector(forward, 1.5).add(new THREE.Vector3(0, -0.6, 0));
  vr.panel.lookAt(eye);
  vr.panel.visible = true;
  drawShelfVR(true);
}

const pointer = new THREE.Raycaster();
pointer.layers.enableAll();

const turned = new THREE.Matrix4();       // one, reused: this runs for both hands every frame

function aim(controller) {
  const rotation = turned.extractRotation(controller.matrixWorld);
  pointer.ray.origin.setFromMatrixPosition(controller.matrixWorld);
  pointer.ray.direction.set(0, 0, -1).applyMatrix4(rotation);
}

const clamp01 = (x) => Math.max(0, Math.min(1, x));

// What a controller points at: the control, and how far along it for a slider or the scan bar.
function aimedAt(controller) {
  if (!vr.ui || !vr.panel || !vr.panel.visible) return null;
  aim(controller);
  const parts = [vr.ui.settings, vr.ui.bar, vr.ui.pill].filter((part) => part.mesh.visible);
  for (const hit of pointer.intersectObjects(parts.map((part) => part.mesh), false)) {
    const part = parts.find((each) => each.mesh === hit.object);
    const spot = part.surface.fromUv(hit.uv.x, hit.uv.y);
    const region = part.surface.at(spot.x, spot.y);
    if (!region) continue;           // clear canvas round a panel: look behind it
    const value = region.x1 != null ? clamp01((spot.x - region.x0) / (region.x1 - region.x0)) : undefined;
    return { part, region, id: region.id, x: spot.x, y: spot.y, value };
  }
  return null;
}

// While dragging only the sideways position matters, so a hand drifting off the bar keeps going.
function along(controller, part, region) {
  aim(controller);
  const hit = pointer.intersectObject(part.mesh, false)[0];
  if (!hit) return null;
  const spot = part.surface.fromUv(hit.uv.x, hit.uv.y);
  return clamp01((spot.x - region.x0) / (region.x1 - region.x0));
}

function press(hit, controller = null) {
  if (hit.region.kind === "scrub") {
    // Nothing moves until the trigger is let go, so a converted stream is not restarted for
    // every step of the drag.
    vr.scrub = { controller, fraction: hit.value, part: hit.part, region: hit.region };
    shelf.scrubHover = hit.value;
    drawShelfVR(true);
    return;
  }
  if (hit.region.kind === "slider") {
    vr.slide = { controller, part: hit.part, region: hit.region, key: hit.id.slice("slider:".length) };
    setSlider(vr.slide.key, hit.value);
    drawShelfVR(true);
    return;
  }
  act(hit.id);
  drawShelfVR(true);
}

// Where a control is, for the tests, which have no hand to point with: the world position of
// its centre, or of a point that far along it.
function shelfPoint(partName, id, fraction = 0.5) {
  const part = vr.ui[partName];
  const region = part.surface.regions.find((r) => r.id === id);
  if (!region) return null;
  const x = region.x1 != null ? region.x0 + fraction * (region.x1 - region.x0) : region.x + region.w / 2;
  const { u, v } = part.surface.toUv(x, region.y + region.h / 2);
  part.mesh.updateMatrixWorld(true);
  return part.mesh.localToWorld(new THREE.Vector3(u - 0.5, v - 0.5, 0));
}

// ---------------------------------------------------------------- a flat film on a screen

function screenMesh(item, eye) {
  const geometry = new THREE.PlaneGeometry(1, 1);
  const uv = geometry.attributes.uv;
  for (let i = 0; i < uv.count; i++) {
    let u = uv.getX(i), v = uv.getY(i);
    if (item.vr_layout === LAYOUT_SBS) u = (u + eye) * 0.5;
    else if (item.vr_layout === LAYOUT_TB) v = (v + eye) * 0.5;
    uv.setXY(i, u, v);
  }
  const material = eyeMaterial(item, eye);
  material.uniforms.fisheye.value = 0;
  const mesh = new THREE.Mesh(geometry, material);
  mesh.userData.screen = true;
  return mesh;
}

function placeScreen() {
  const item = vr.item;
  if (!item || item.is_vr) return;
  const across = (item.width || 16) / (item.vr_layout === LAYOUT_SBS ? 2 : 1);
  const down = (item.height || 9) / (item.vr_layout === LAYOUT_TB ? 2 : 1);
  const width = screenWidth();
  for (const mesh of vr.meshes) {
    mesh.scale.set(width, width * down / across, 1);
    mesh.position.set(0, 0, -screenDistance());
  }
}

// ---------------------------------------------------------------- the world around it

function applyWorld() {
  if (!vr.scene) return;
  if (vr.world) {
    vr.scene.remove(vr.world);
    disposeWorld(vr.world);
    vr.world = null;
  }
  const item = vr.item;
  // Only in the headset, not when your own room is what is wanted behind the film, and not round
  // a 360 film, which covers everything anyway. Behind a cut-out it is what shows through.
  if (vr.mode !== "immersive" || shelf.env === "passthrough" || !item) return;
  if (item.is_vr && item.vr_proj === PROJ_360) return;
  vr.world = buildWorld(THREE, shelf.env);
  vr.scene.add(vr.world);
}

// ---------------------------------------------------------------- controllers

function pointing(controller) {
  const facing = new THREE.Quaternion();
  controller.getWorldQuaternion(facing);
  const direction = new THREE.Vector3(0, 0, -1).applyQuaternion(facing);
  // A positive turn moves the picture left, so pointing further left must give a larger angle.
  return { yaw: Math.atan2(-direction.x, -direction.z), pitch: Math.asin(Math.max(-1, Math.min(1, direction.y))) };
}

function ensureControllers() {
  if (vr.controllers.length) return;
  for (let index = 0; index < 2; index++) {
    const controller = vr.renderer.xr.getController(index);
    controller.layers.enableAll();
    const beam = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, -6)]),
      new THREE.LineBasicMaterial({ color: 0xf2545b, transparent: true, depthTest: false }));
    beam.layers.enableAll();
    beam.renderOrder = 40;            // after the film, which is drawn over everything behind it
    controller.add(beam);

    controller.addEventListener("selectstart", () => {
      const hit = aimedAt(controller);
      if (!vr.panel.visible || !hit) {
        placePanel();  // a press with nothing aimed at brings the panel back in front
        return;
      }
      press(hit, controller);
    });
    controller.addEventListener("selectend", () => {
      if (vr.slide && vr.slide.controller === controller) vr.slide = null;
      if (!vr.scrub || vr.scrub.controller !== controller) return;
      const total = totalLength();
      if (total) seekTo(vr.scrub.fraction * total);
      vr.scrub = null;
      shelf.scrubHover = null;
      drawShelfVR(true);
    });
    // Holding the grip grabs the picture: move the hand and the view follows it.
    controller.addEventListener("squeezestart", () => {
      vr.grab = { controller, start: pointing(controller), yaw: vr.view.yaw, pitch: vr.view.pitch };
    });
    controller.addEventListener("squeezeend", () => {
      if (vr.grab && vr.grab.controller === controller) {
        vr.grab = null;
        saveView();
      }
    });
    vr.scene.add(controller);
    vr.controllers.push(controller);
  }
}

const DEADZONE = 0.25;

function togglePanel() {
  if (vr.panel.visible) vr.panel.visible = false;
  else placePanel();
}

function oncePressed(key, down) {
  const was = vr.pressed[key];
  vr.pressed[key] = down;
  return down && !was;
}

// Thumbsticks and buttons have no events, so they are read every frame.
function readSticks(dt) {
  const session = vr.renderer.xr.getSession();
  if (!session) return;
  let seekStick = 0;
  for (const source of session.inputSources) {
    const pad = source.gamepad;
    if (!pad) continue;
    const x = pad.axes.length >= 4 ? pad.axes[2] : pad.axes[0] || 0;
    const y = pad.axes.length >= 4 ? pad.axes[3] : pad.axes[1] || 0;
    const hand = source.handedness;
    if (hand === "right") {
      if (Math.abs(x) > DEADZONE && Math.abs(x) >= Math.abs(y)) seekStick = x;
      else if (Math.abs(y) > DEADZONE) adjustView({ zoom: Math.exp(-y * 1.1 * dt) });
      if (oncePressed("right-a", pad.buttons[4] && pad.buttons[4].pressed)) act("play");
      // B hides and shows the panel, matching X on the left hand, so either controller on its
      // own can do it. It used to reset the view - but that is now on the panel itself and on
      // the headset's own recentre, while a hidden panel cannot be pressed to bring itself back.
      if (oncePressed("right-b", pad.buttons[5] && pad.buttons[5].pressed)) togglePanel();
    } else if (hand === "left") {
      if (Math.abs(x) > DEADZONE) adjustView({ yaw: -x * 70 * DEG * dt });
      if (Math.abs(y) > DEADZONE) adjustView({ pitch: -y * 50 * DEG * dt });
      if (oncePressed("left-x", pad.buttons[4] && pad.buttons[4].pressed)) togglePanel();
    }
    // Pressing the stick works on either controller. Hiding the panel is the one thing that
    // cannot be done from the panel, so somebody holding a single controller - or able to
    // use only one hand - must still be able to bring it back. A and B on the right are
    // already spoken for, and the stick press is free on both.
    if (oncePressed(`${hand}-stick`, pad.buttons[3] && pad.buttons[3].pressed)) togglePanel();
  }

  // Pushing the right stick sideways runs through the video - gently at first, quickly at the
  // edge - and jumps when it is let go.
  const total = totalLength();
  if (seekStick && total) {
    if (!vr.stickSeek) {
      vr.stickSeek = { time: shownTime() };
      if (!vr.panel.visible) placePanel();
    }
    const push = (Math.abs(seekStick) - DEADZONE) / (1 - DEADZONE);
    const speed = 8 + 112 * push * push;  // seconds of video per second held
    vr.stickSeek.time = Math.max(0, Math.min(total, vr.stickSeek.time + Math.sign(seekStick) * speed * dt));
  } else if (vr.stickSeek) {
    seekTo(vr.stickSeek.time);
    vr.stickSeek = null;
  }

  if (vr.grab) {
    const now = pointing(vr.grab.controller);
    vr.view.yaw = vr.grab.yaw + (now.yaw - vr.grab.start.yaw);
    vr.view.pitch = vr.grab.pitch + (now.pitch - vr.grab.start.pitch);
    applyView();
  }
}

function drawImmersive(now) {
  const dt = vr.lastFrame ? Math.min(0.1, (now - vr.lastFrame) / 1000) : 0;
  vr.lastFrame = now;
  syncMask();
  readSticks(dt);
  if (vr.panel && vr.panel.visible) {
    let hover = null;
    for (const controller of vr.controllers) {
      const hit = aimedAt(controller);
      if (hit) hover = hit;
    }
    if (vr.scrub) {
      const fraction = along(vr.scrub.controller, vr.scrub.part, vr.scrub.region);
      if (fraction != null) vr.scrub.fraction = fraction;
      shelf.scrubHover = vr.scrub.fraction;
    } else {
      shelf.scrubHover = hover && hover.region.kind === "scrub" ? hover.value : null;
    }
    if (vr.slide) {
      const fraction = along(vr.slide.controller, vr.slide.part, vr.slide.region);
      if (fraction != null) setSlider(vr.slide.key, fraction);
    }
    shelf.hover = hover ? hover.id : null;
    drawShelfVR();       // repaints only what has changed
  }
  vr.renderer.render(vr.scene, vr.camera);
}

function leaveVR() {
  const session = vr.renderer && vr.renderer.xr.getSession();
  if (session) session.end();
}

function isPassthrough(item) {
  const words = ((item && item.tags) || []).join(" ") + " " + ((item && item.title) || "");
  return /passthrough|alpha/i.test(words);
}

async function supportsAR() {
  if (!navigator.xr) return false;
  try {
    return await navigator.xr.isSessionSupported("immersive-ar");
  } catch (error) {
    return false;
  }
}

async function vrSupported() {
  if (!navigator.xr) return false;
  try {
    return await navigator.xr.isSessionSupported("immersive-vr");
  } catch (error) {
    return false;
  }
}

// How sharp the headset draws. three.js asks for the headset's default resolution, which on a
// Quest is well under its panels' own, and turns fixed foveation fully on - everything away
// from the middle of the view drawn at a fraction of the resolution, which reads as the
// lenses being worse than in DeoVR. Games need that to hold their frame rate; this draws one
// sphere, so it can have the panels' full resolution everywhere. Capped, in case a headset
// reports something extravagant. Must be set before the session is handed to three.js.
const SHARPEST = 1.5;

function sharpen(session) {
  let scale = 1;
  try {
    const native = window.XRWebGLLayer && XRWebGLLayer.getNativeFramebufferScaleFactor
      ? XRWebGLLayer.getNativeFramebufferScaleFactor(session) : 1;
    if (Number.isFinite(native) && native > 0) scale = Math.max(1, Math.min(native, SHARPEST));
  } catch (error) { /* the default resolution, then */ }
  vr.renderer.xr.setFramebufferScaleFactor(scale);
  vr.renderer.xr.setFoveation(0);
  return scale;
}

async function enterVR() {
  const item = state.current;
  if (!item) return;
  if (!navigator.xr) {
    toast(location.protocol === "https:" ? "This browser has no VR mode"
                                         : "VR needs the https address - see BlueShelf on your PC", 5000);
    return;
  }
  if (!ensureRenderer()) return;
  // A scene shot for passthrough opens with its cut-out mask already on, so it simply works.
  if (vr.item !== item) vr.see.mode = isPassthrough(item) ? SEE_MASK : SEE_OFF;
  buildScene(item, true);
  buildPanel();
  ensureControllers();
  // Showing the room needs a different kind of session, asked for at the start and not changeable
  // afterwards, so which one depends on whether passthrough is wanted now.
  const wantRoom = vr.see.mode !== SEE_OFF && shelf.env === "passthrough";
  const kind = wantRoom && await supportsAR() ? "immersive-ar" : "immersive-vr";
  try {
    const session = await navigator.xr.requestSession(kind,
                                                      { optionalFeatures: ["local-floor", "hand-tracking"] });
    vr.inAR = kind === "immersive-ar";
    vr.renderer.setClearAlpha(vr.inAR ? 0 : 1);
    vr.mode = "immersive";
    vr.lastFrame = 0;
    video.classList.add("as-source");
    stage().hidden = false;
    sharpen(session);
    await vr.renderer.xr.setSession(session);
    vr.renderer.setAnimationLoop(drawImmersive);
    applyWorld();
    // Holding the headset's own button recentres the room, and people reasonably expect the
    // picture to come round with it. Without this it does not: the turn and tilt applied here
    // stay on top of the new forward, so a picture nudged thirty degrees left stays thirty
    // degrees left of wherever you are now facing. The zoom is left alone - recentring is
    // about which way you are pointed, and somebody who zoomed in meant it.
    const space = vr.renderer.xr.getReferenceSpace();
    if (space && space.addEventListener) {
      space.addEventListener("reset", () => {
        vr.view.yaw = vr.view.pitch = vr.view.roll = 0;
        applyView();
        saveView();
        if (vr.panel && vr.panel.visible) placePanel();
      });
    }
    placePanel();
    session.addEventListener("end", () => {
      vr.renderer.setAnimationLoop(null);
      if (vr.panel) vr.panel.visible = false;
      vr.scrub = vr.stickSeek = vr.grab = null;
      vr.inAR = false;
      vr.renderer.setClearAlpha(1);
      vr.mode = "off";
      applyWorld();
      if (state.current) {
        if (state.current.is_vr) startFlat(state.current);
        else stopFlat();
      } else {
        stopFlat();
      }
    });
  } catch (error) {
    toast("Could not start VR: " + error.message, 5000);
    if (item.is_vr) startFlat(item);
  }
}


// ?debug on the address opens the VR machinery up to the test harness, which has no headset to
// press buttons with. It costs nothing on a normal visit.
if (new URLSearchParams(location.search).has("debug")) {
  window.blueshelfDebug = { vr, THREE, shelf, buildScene, buildPanel, drawShelfVR, placePanel, aimedAt, press,
                            shelfPoint, adjustView, resetView, applyView, isPassthrough, act, shelfModel,
                            SEE_OFF, SEE_MASK, SEE_BACKDROP, SEE_COLOUR, PROJECTIONS, lensName, projectionOf,
                            attachMask, playMask, setMask, chosenMask, syncMask, detachMask, video, state,
                            sharpen, SHARPEST, ShelfVR, applyWorld, screenWidth, screenDistance };
}

// ---------------------------------------------------------------- start

const THEME_KEY = "blueshelf-theme";
const THEME_ORDER = { system: "light", light: "dark", dark: "system" };

function applyTheme(mode) {
  if (mode === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
}

state.quality = remember.get(QUALITY_KEY) || "auto";
buildShelfPage();
let theme = remember.get(THEME_KEY) || "system";
applyTheme(theme);
el("theme-btn").onclick = () => {
  theme = THEME_ORDER[theme] || "system";
  remember.set(THEME_KEY, theme);
  applyTheme(theme);
  toast(theme === "system" ? "Following the headset setting" : `${theme[0].toUpperCase()}${theme.slice(1)} mode`);
};

el("menu-btn").onclick = () => el("side").classList.toggle("hidden");
el("search").oninput = (event) => {
  state.search = event.target.value.trim();
  clearTimeout(el("search").timer);
  el("search").timer = setTimeout(loadLibrary, 250);
};
for (const button of document.querySelectorAll(".side-item[data-view]")) {
  button.onclick = () => {
    state.view = button.dataset.view;
    state.performer = null;
    for (const item of document.querySelectorAll(".side-item")) item.classList.remove("active");
    button.classList.add("active");
    loadLibrary();
  };
}
// Keys, so a keyboard or a headset controller with one can drive it without aiming at buttons.
document.addEventListener("keydown", (event) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);
  if (typing || el("player").hidden) return;
  const jump = event.shiftKey ? 1 : event.ctrlKey ? 30 : 5;
  const actions = {
    " ": () => act("play"),
    k: () => act("play"),
    ArrowLeft: () => (event.altKey ? step(-1) : seekTo(state.offset + video.currentTime - jump)),
    ArrowRight: () => (event.altKey ? step(1) : seekTo(state.offset + video.currentTime + jump)),
    ArrowUp: () => setVolume(video.volume + 0.1),
    ArrowDown: () => setVolume(video.volume - 0.1),
    PageUp: () => step(-1),
    PageDown: () => step(1),
    p: () => step(-1),
    n: () => step(1),
    m: () => act("mute"),
    f: () => act("full"),
    v: () => act("vr"),
    e: () => toggleEye(),
    s: () => act("settings"),
    t: () => act("mark"),
    Home: () => seekTo(0),
    Escape: () => (shelf.settings ? act("close") : closePlayer()),
    Delete: () => act("delete"),
  };
  const action = actions[event.key];
  if (action) {
    event.preventDefault();
    action();
  }
});

// Look for newly added videos: every half minute while the library is on screen, and at once
// when the page comes back to view, which is when someone has just finished a download.
const REFRESH_SECONDS = 30;

function refreshIfIdle() {
  if (document.hidden || !el("player").hidden) return;  // never while watching something
  loadLibrary(true).catch(() => {});
  loadPerformers().catch(() => {});
}

setInterval(refreshIfIdle, REFRESH_SECONDS * 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshIfIdle();
});

window.addEventListener("pagehide", saveResume);

loadPerformers();
loadLibrary();
