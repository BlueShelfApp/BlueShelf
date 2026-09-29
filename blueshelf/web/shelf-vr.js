// The Shelf player controls, drawn for the headset.
//
// A headset's browser shows no page inside VR, so the controls are painted onto canvases that
// hang in front of you, and every drawing call that makes something pointable records where it
// is. Pointing then asks the canvas what is under the ray - there is no separate hit-test map to
// fall out of step with what is drawn. Sizes are the design's own pixels (the Shelf mock-up of
// the Blueshelf Quest player); the page draws the same thing as ordinary HTML.

import { drawIcon } from "./icons.js";

export const C = {
  bg: "#f5ead8", surface: "#ebddc5", text: "#201e1d",
  accent: "#c67139", accentHover: "#b2622d", accentPressed: "#8c491a",
  n300: "#dcd3c4", n400: "#c0b6a5", n500: "#a19786", n700: "#645c50", n800: "#474238",
  a100: "#fff2eb", a200: "#ffe1d0", a300: "#ffc6a5", a700: "#8c491a", a800: "#643312",
  s200: "#e1eecc", s800: "#3d472b",
  tint: "rgba(32,30,29,.08)", line: "rgba(32,30,29,.16)",
};
const HEAD = "Caprasimo, Georgia, serif";
const BODY = "Figtree, system-ui, sans-serif";
const font = (size, weight = 400, face = BODY) => `${weight} ${size}px ${face}`;

// ---------------------------------------------------------------- a canvas you can point at

export class Surface {
  // 1.25 canvas pixels to each of the design's: about one to one with the headset's own pixels
  // at arm's length, so text is sharp without mip-maps, which cost too much to rebuild.
  constructor(width, height, { scale = 1.25, margin = 0, anchor = "top" } = {}) {
    this.width = width;           // the drawable area, in design pixels
    this.height = height;
    this.margin = margin;         // room around it for the shadow, never pointable
    this.scale = scale;           // canvas pixels per design pixel, for sharp text up close
    this.canvas = document.createElement("canvas");
    this.canvas.width = Math.round((width + 2 * margin) * scale);
    this.canvas.height = Math.round((height + 2 * margin) * scale);
    this.ctx = this.canvas.getContext("2d");
    this.regions = [];
    this.shown = height;          // how much of the height the current content uses
    // A panel whose height changes - settings, a tab at a time - is drawn against the bottom
    // of its canvas, so its bottom edge stays put above the bar whatever is showing.
    this.anchor = anchor;
    this.offsetY = 0;
  }

  begin(shown = this.height) {
    const ctx = this.ctx;
    this.shown = shown;
    this.regions = [];
    this.offsetY = this.anchor === "bottom" ? this.height - shown : 0;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.setTransform(this.scale, 0, 0, this.scale, this.margin * this.scale,
                     (this.margin + this.offsetY) * this.scale);
    return ctx;
  }

  region(id, x, y, w, h, extra = {}) {
    this.regions.push({ id, x, y, w, h, ...extra });
  }

  // What is at (x, y) in design pixels: the last thing drawn there, as it is the one on top.
  at(x, y) {
    for (let i = this.regions.length - 1; i >= 0; i--) {
      const r = this.regions[i];
      if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return r;
    }
    return null;
  }

  // A point on the plane, as three.js reports it, in the drawing's own pixels.
  fromUv(u, v) {
    return { x: u * (this.width + 2 * this.margin) - this.margin,
             y: (1 - v) * (this.height + 2 * this.margin) - this.margin - this.offsetY };
  }

  // The other way: where a drawing's pixel is on the plane, from its top left, as fractions.
  toUv(x, y) {
    return { u: (this.margin + x) / (this.width + 2 * this.margin),
             v: 1 - (this.margin + this.offsetY + y) / (this.height + 2 * this.margin) };
  }

  centre(id) {
    const r = this.regions.find((each) => each.id === id);
    return r ? { x: r.x + r.w / 2, y: r.y + r.h / 2 } : null;
  }
}

// ---------------------------------------------------------------- drawing helpers

function shape(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, Math.min(r, h / 2, w / 2));
}

function fill(ctx, colour, x, y, w, h, r) {
  shape(ctx, x, y, w, h, r);
  ctx.fillStyle = colour;
  ctx.fill();
}

function write(ctx, text, x, y, style, colour, align = "left", baseline = "middle") {
  ctx.font = style;
  ctx.fillStyle = colour;
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  ctx.fillText(text, x, y);
}

function fitted(ctx, text, style, room) {
  ctx.font = style;
  if (ctx.measureText(text).width <= room) return text;
  let cut = text;
  while (cut.length > 1 && ctx.measureText(`${cut}…`).width > room) cut = cut.slice(0, -1);
  return `${cut.trimEnd()}…`;
}

// Times drawn with every digit the same width, so a clock ticking does not jiggle the text
// beside it - the design asks for tabular figures, and a canvas has no switch for them.
function clockText(ctx, text, right, y, style, colour) {
  ctx.font = style;
  const digit = Math.max(...[..."0123456789"].map((d) => ctx.measureText(d).width));
  const widths = [...text].map((ch) => (/\d/.test(ch) ? digit : ctx.measureText(ch).width));
  let x = right - widths.reduce((a, b) => a + b, 0);
  ctx.fillStyle = colour;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  [...text].forEach((ch, i) => {
    ctx.fillText(ch, x + widths[i] / 2, y);
    x += widths[i];
  });
  return right - widths.reduce((a, b) => a + b, 0);
}

// A panel's rounded shape and its soft shadow, drawn once and then copied. A blurred shadow is
// one of the slowest things a canvas does, and the bar is repainted every time its clock ticks.
const backdrops = new Map();
function backdrop(S, w, h, r, colour, shadow = "rgba(0,0,0,.45)") {
  const pad = 40;
  const key = [w, h, r, colour, shadow, S.scale].join("|");
  let picture = backdrops.get(key);
  if (!picture) {
    picture = document.createElement("canvas");
    picture.width = Math.ceil((w + 2 * pad) * S.scale);
    picture.height = Math.ceil((h + 2 * pad) * S.scale);
    const c = picture.getContext("2d");
    c.setTransform(S.scale, 0, 0, S.scale, pad * S.scale, pad * S.scale);
    shadowed(c, shadow, 40, 14, () => fill(c, colour, 0, 0, w, h, r));
    backdrops.set(key, picture);
  }
  S.ctx.drawImage(picture, -pad, -pad, w + 2 * pad, h + 2 * pad);
}

function shadowed(ctx, colour, blur, dy, draw) {
  ctx.save();
  ctx.shadowColor = colour;
  ctx.shadowBlur = blur;
  ctx.shadowOffsetY = dy;
  draw();
  ctx.restore();
}

// A round icon button. `look` is {bg, fg}; hover darkens a clear one the way the design does.
function roundButton(S, id, x, y, size, icon, look, hover, iconSize = 24) {
  const ctx = S.ctx;
  const bg = hover === id && look.hover ? look.hover
           : hover === id && look.bg === "transparent" ? C.tint : look.bg;
  if (bg !== "transparent") fill(ctx, bg, x, y, size, size, size / 2);
  drawIcon(ctx, icon, x + (size - iconSize) / 2, y + (size - iconSize) / 2, iconSize, look.fg);
  S.region(id, x, y, size, size);
}

function chipLook(on) {
  return on ? { bg: C.s200, fg: C.s800 } : { bg: "transparent", fg: C.text };
}

function pillText(S, id, text, x, y, style, look, hover, padX, h) {
  const ctx = S.ctx;
  ctx.font = style;
  const w = ctx.measureText(text).width + padX * 2;
  const bg = hover === id && look.hover ? look.hover : look.bg;
  if (bg !== "transparent") fill(ctx, bg, x, y, w, h, h / 2);
  if (look.border) {
    shape(ctx, x + 0.5, y + 0.5, w - 1, h - 1, h / 2);
    ctx.strokeStyle = look.border;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  write(ctx, text, x + w / 2, y + h / 2 + 0.5, style, look.fg, "center");
  if (id) S.region(id, x, y, w, h);
  return w;
}

// ---------------------------------------------------------------- the bar

export const BAR = { w: 980, h: 200, padX: 30, trackY: 74, trackH: 10, rowY: 104 };

export function drawBar(S, m) {
  const ctx = S.begin(BAR.h);
  const hover = m.hover;
  backdrop(S, BAR.w, BAR.h, 44, C.bg);

  // The title row: what is playing on the left, its shape and the time on the right.
  const right = BAR.w - BAR.padX;
  let x = clockText(ctx, `${m.cur} / ${m.total}`, right, 38, font(15), C.n800) - 14;
  const chip = (id, text, look) => {
    ctx.font = font(13, 700);
    const w = ctx.measureText(text).width + 24;
    x -= w;
    pillText(S, id, text, x, 24, font(13, 700), look, hover, 12, 28);
    x -= 8;
  };
  if (m.stereo) chip(null, m.stereo, { bg: C.s200, fg: C.s800 });
  if (m.lens) chip("lensChip", m.lens, { bg: C.a200, fg: C.a800, hover: C.a300 });
  chip("mark", m.marking ? `End scene · ${m.marking.at}` : "Mark scene",
       m.marking ? { bg: C.accent, fg: C.bg, hover: C.accentHover } : { bg: C.s200, fg: C.s800, hover: "#ccdbb2" });
  // Not in the design, but nothing BlueShelf does is dropped: leaving VR, favourite, delete.
  if (m.leave) {
    ctx.font = font(13, 700);
    const w = ctx.measureText("Leave VR").width + 24;
    x -= w;
    pillText(S, "leave", "Leave VR", x, 24, font(13, 700), { bg: C.text, fg: C.bg, hover: C.n800 }, hover, 12, 28);
    x -= 8;
  }
  x -= 32;
  roundButton(S, "delete", x, 22, 32, "trash",
              m.armed ? { bg: C.accent, fg: C.bg } : { bg: "transparent", fg: C.text }, hover, 18);
  x -= 4 + 32;
  roundButton(S, "fav", x, 22, 32, m.fav ? "heartFull" : "heart",
              { bg: "transparent", fg: m.fav ? C.accent : C.text }, hover, 18);
  x -= 12;

  const title = fitted(ctx, m.title || "", font(24, 400, HEAD), Math.max(80, (x - BAR.padX) * 0.62));
  write(ctx, title, BAR.padX, 46, font(24, 400, HEAD), C.text, "left", "alphabetic");
  const after = BAR.padX + ctx.measureText(title).width + 14;
  if (m.subtitle && x - after > 40) {
    write(ctx, fitted(ctx, m.subtitle, font(15), x - after), after, 46, font(15), C.n700, "left", "alphabetic");
  }

  // The scan bar: bought, played, chapters, where you are, and where you are pointing.
  const x0 = BAR.padX, span = BAR.w - 2 * BAR.padX, ty = BAR.trackY, th = BAR.trackH;
  fill(ctx, C.n300, x0, ty, span, th, th / 2);
  if (m.buffered > 0) fill(ctx, C.n400, x0, ty, span * Math.min(1, m.buffered), th, th / 2);
  if (m.progress > 0) fill(ctx, C.accent, x0, ty, span * Math.min(1, m.progress), th, th / 2);
  ctx.fillStyle = C.bg;
  for (const tick of m.ticks || []) ctx.fillRect(x0 + span * tick, ty, 3, th);
  for (const scene of m.scenes || []) {
    fill(ctx, "#7a8a5e", x0 + span * scene.from, ty + th + 4, Math.max(3, span * (scene.to - scene.from)), 4, 2);
  }
  if (m.marking) {
    ctx.globalAlpha = 0.6;
    fill(ctx, C.accent, x0 + span * m.marking.from, ty + th + 4, Math.max(3, span * (m.marking.to - m.marking.from)), 4, 2);
    ctx.globalAlpha = 1;
  }
  const knob = x0 + span * Math.min(1, Math.max(0, m.progress || 0));
  shadowed(ctx, "rgba(46,43,37,.3)", 6, 2, () => {
    ctx.beginPath();
    ctx.arc(knob, ty + th / 2, 12, 0, Math.PI * 2);
    ctx.fillStyle = C.bg;
    ctx.fill();
  });
  ctx.beginPath();
  ctx.arc(knob, ty + th / 2, 8, 0, Math.PI * 2);
  ctx.fillStyle = C.accent;
  ctx.fill();
  if (m.scrubHover != null) {
    fill(ctx, C.text, x0 + span * m.scrubHover - 1.5, ty - 4, 3, th + 8, 2);
  }
  S.region("scrub", x0, 60, span, 38, { kind: "scrub", x0, x1: x0 + span });

  // The row of buttons: volume, the transport in the middle, the tools.
  const cy = BAR.rowY + 40, top = cy - 27;
  const vx = 30;
  if (hover === "volume") fill(ctx, C.tint, vx, top, 135, 54, 27);
  drawIcon(ctx, m.muted ? "mute" : "volume", vx + 15, cy - 12, 24, C.text);
  fill(ctx, C.n300, vx + 49, cy - 3, 70, 6, 3);
  if (!m.muted) fill(ctx, C.text, vx + 49, cy - 3, 70 * Math.max(0, Math.min(1, m.volume)), 6, 3);
  S.region("volume", vx, top, 135, 54);

  roundButton(S, "prev", 326, top, 54, "prev", { bg: "transparent", fg: C.text }, hover);
  skipButton(S, "back10", 388, top, "ccw", hover);
  fill(ctx, "rgba(198,113,57,.16)", 446, cy - 36, 88, 88, 44);      // the glow under it, flat
  fill(ctx, hover === "play" ? C.accentHover : C.accent, 450, cy - 40, 80, 80, 40);
  drawIcon(ctx, m.playing ? "pause" : "play", 475, cy - 15, 30, C.bg);
  S.region("play", 450, cy - 40, 80, 80);
  skipButton(S, "fwd10", 538, top, "cw", hover);
  roundButton(S, "next", 600, top, 54, "next", { bg: "transparent", fg: C.text }, hover);

  roundButton(S, "threeD", 656, top, 54, "box", m.threeDAvailable ? chipLook(m.threeD) : { bg: "transparent", fg: C.n500 }, hover);
  roundButton(S, "passthrough", 716, top, 54, "eye", chipLook(m.passthrough), hover);
  roundButton(S, "recenter", 776, top, 54, "locate", { bg: "transparent", fg: C.text }, hover);
  roundButton(S, "settings", 836, top, 54, "sliders",
              m.settings ? { bg: C.accent, fg: C.bg } : { bg: "transparent", fg: C.text }, hover);
  roundButton(S, "collapse", 896, top, 54, "down", { bg: C.surface, fg: C.text, hover: C.n300 }, hover);
}

function skipButton(S, id, x, y, icon, hover) {
  const ctx = S.ctx;
  if (hover === id) fill(ctx, C.tint, x, y, 54, 54, 27);
  drawIcon(ctx, icon, x + 12, y + 12, 30, C.text);
  write(ctx, "10", x + 27, y + 27, font(10, 700), C.text, "center");
  S.region(id, x, y, 54, 54);
}

// ---------------------------------------------------------------- the pointing bubble

export const BUBBLE = { w: 232, h: 166 };

export function drawBubble(S, m, strip) {
  const ctx = S.begin(BUBBLE.h);
  backdrop(S, BUBBLE.w, BUBBLE.h, 24, C.text, "rgba(0,0,0,.4)");
  // The frame at that point, when the film has had its strip of stills made.
  ctx.save();
  shape(ctx, 6, 6, 220, 122, 18);
  ctx.clip();
  if (strip && strip.image && strip.image.complete && strip.image.naturalWidth) {
    const n = Math.min(strip.count - 1, Math.floor(m.scrubHover * strip.count));
    const col = n % strip.cols, row = Math.floor(n / strip.cols);
    ctx.drawImage(strip.image, col * strip.tileW, row * strip.tileH, strip.tileW, strip.tileH, 6, 6, 220, 122);
  } else {
    ctx.fillStyle = C.n800;
    ctx.fillRect(6, 6, 220, 122);
    if (m.stillsNote) write(ctx, m.stillsNote, 116, 67, font(12, 600), C.n400, "center");
  }
  ctx.restore();
  write(ctx, m.hoverTime || "", 16, 147, font(15, 700), C.bg);
  if (m.hoverChapter) {
    write(ctx, fitted(ctx, m.hoverChapter, font(13), 120), 216, 147, font(13), C.n400, "right");
  }
}

// ---------------------------------------------------------------- folded away

export const PILL = { w: 374, h: 72 };

export function drawCollapsed(S, m) {
  const ctx = S.begin(PILL.h);
  const hover = m.hover;
  backdrop(S, PILL.w, PILL.h, 36, C.bg);
  fill(ctx, hover === "play" ? C.accentHover : C.accent, 8, 8, 56, 56, 28);
  drawIcon(ctx, m.playing ? "pause" : "play", 8 + 17, 8 + 17, 22, C.bg);
  S.region("play", 8, 8, 56, 56);
  ctx.font = font(14, 700);
  const title = fitted(ctx, m.title || "", font(14, 700), 150);
  write(ctx, title, 78, 30, font(14, 700), C.text);
  write(ctx, `· ${m.cur}`, 78 + ctx.measureText(title).width + 5, 30, font(14), C.n700);
  fill(ctx, C.n300, 78, 42, 220, 6, 3);
  fill(ctx, C.accent, 78, 42, 220 * Math.min(1, m.progress || 0), 6, 3);
  roundButton(S, "expand", 312, 10, 52, "up", { bg: C.surface, fg: C.text, hover: C.n300 }, hover);
}

// ---------------------------------------------------------------- paused

export const PAUSED = { w: 760, h: 150 };

export function drawPaused(S, m) {
  const ctx = S.begin(PAUSED.h);
  ctx.shadowColor = "rgba(0,0,0,.6)";
  ctx.shadowBlur = 12;
  write(ctx, "PAUSED", 0, 16, font(13, 700), C.a300);
  write(ctx, fitted(ctx, m.title || "", font(44, 400, HEAD), PAUSED.w), 0, 66, font(44, 400, HEAD), C.bg);
  const line = [m.subtitle, m.remain ? `${m.remain} left` : ""].filter(Boolean).join(" · ");
  write(ctx, fitted(ctx, line, font(17), PAUSED.w), 0, 112, font(17), "#dcd3c4");
  ctx.shadowColor = "transparent";
}

// ---------------------------------------------------------------- settings

export const SETTINGS = { w: 760, maxH: 560 };
const TABS = [["projection", "Projection"], ["screen", "Screen"], ["audio", "Audio"], ["env", "Environment"],
              ["scenes", "Scenes"]];
const SCENE_ROWS = 6;          // as many as fit; the rest are on the PC

export function drawSettings(S, m) {
  const ctx = S.ctx;
  const hover = m.hover;
  // Measure first by drawing nowhere would cost a second pass; the height is worked out per tab.
  const height = settingsHeight(m);
  S.begin(height);
  backdrop(S, SETTINGS.w, height, 34, C.bg, "rgba(0,0,0,.5)");

  write(ctx, "Settings", 22 + 12, 22 + 4 + 13, font(22, 400, HEAD), C.text);
  TABS.forEach(([key, label], i) => {
    const y = 22 + 40 + i * 46;
    const on = m.tab === key;
    const bg = on ? C.text : hover === `tab:${key}` ? C.tint : "transparent";
    if (bg !== "transparent") fill(ctx, bg, 22, y, 176, 40, 20);
    write(ctx, label, 22 + 16, y + 20.5, font(15, 600), on ? C.bg : C.text);
    S.region(`tab:${key}`, 22, y, 176, 40);
  });

  const x0 = 22 + 176 + 20, w = SETTINGS.w - 22 - x0;
  const label = (TABS.find(([key]) => key === m.tab) || TABS[0])[1];
  ctx.save();
  ctx.font = font(12, 700);
  ctx.letterSpacing = "1.2px";
  write(ctx, label.toUpperCase(), x0, 22 + 22, font(12, 700), C.a700);
  ctx.restore();
  roundButton(S, "close", SETTINGS.w - 22 - 44, 22, 44, "x", { bg: C.surface, fg: C.text, hover: C.n300 }, hover, 20);

  let y = 22 + 44 + 14;
  if (m.tab === "projection") y = projectionTab(S, m, x0, y, w);
  else if (m.tab === "screen") y = screenTab(S, m, x0, y, w);
  else if (m.tab === "audio") y = audioTab(S, m, x0, y, w);
  else if (m.tab === "scenes") y = scenesTab(S, m, x0, y, w);
  else y = envTab(S, m, x0, y, w);
  return height;
}

function settingsHeight(m) {
  const top = 22 + 44 + 14, bottom = 22;
  const tabsTall = 22 + 40 + TABS.length * 46 + 22;
  let body = 0;
  if (m.tab === "projection") body = gridHeight(m.projections.length, 68) + 18 + 45;
  else if (m.tab === "screen") body = 4 + m.screen.sliders.length * 70 + 48 + (m.screen.quality ? 12 + 22 + 44 : 0) + (m.screen.hint ? 34 : 0);
  else if (m.tab === "audio") body = 70 + 44;
  else if (m.tab === "scenes") {
    const rows = Math.min(SCENE_ROWS, (m.scenes || []).length);
    body = (rows ? rows * 56 - 8 : 24) + ((m.scenes || []).length > SCENE_ROWS ? 26 : 0) + 14 + 36;
  }
  else body = gridHeight(m.envs.length, 115) + (m.passthroughOptions ? 14 + 30 + 44 + (m.passthroughOptions.masks.length ? 30 + 44 : 0) : 0);
  return Math.min(SETTINGS.maxH, Math.max(tabsTall, top + body + bottom));
}

const gridHeight = (count, card) => {
  const rows = Math.ceil(count / 3);
  return rows * card + Math.max(0, rows - 1) * 10;
};

function projectionTab(S, m, x0, y, w) {
  const ctx = S.ctx, hover = m.hover;
  const cw = (w - 20) / 3;
  m.projections.forEach((o, i) => {
    const cx = x0 + (i % 3) * (cw + 10), cy = y + Math.floor(i / 3) * 78;
    const id = `proj:${o.key}`;
    fill(ctx, o.on ? C.a100 : hover === id ? C.n300 : C.surface, cx, cy, cw, 68, 22);
    if (o.on) {
      shape(ctx, cx + 1, cy + 1, cw - 2, 66, 21);
      ctx.strokeStyle = C.accent;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    lensGlyph(ctx, cx + 12, cy + 17, o.deg, o.on ? C.accent : C.n500);
    write(ctx, fitted(ctx, o.label, font(15, 700), cw - 66), cx + 56, cy + 27, font(15, 700), C.text);
    write(ctx, fitted(ctx, o.sub, font(12), cw - 66), cx + 56, cy + 45, font(12), C.n700);
    S.region(id, cx, cy, cw, 68);
  });
  y += gridHeight(m.projections.length, 68) + 18;
  write(ctx, "Stereo format", x0, y + 22, font(14, 600), C.text);
  segmented(S, m.stereos, "stereo", x0 + 124, y, hover);
  return y + 45;
}

// A disc with the lens's field of view cut out of it, the design's conic badge; a rectangle for flat.
function lensGlyph(ctx, x, y, deg, colour) {
  if (!deg) {
    fill(ctx, C.n300, x, y, 34, 34, 7);
    ctx.fillStyle = colour;
    ctx.fillRect(x + 3, y + 8.5, 28, 17);
    return;
  }
  ctx.beginPath();
  ctx.arc(x + 17, y + 17, 17, 0, Math.PI * 2);
  ctx.fillStyle = C.n300;
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(x + 17, y + 17);
  const start = -Math.PI / 2 - (deg / 2) * Math.PI / 180;
  ctx.arc(x + 17, y + 17, 17, start, start + Math.min(deg, 359.9) * Math.PI / 180);
  ctx.closePath();
  ctx.fillStyle = colour;
  ctx.fill();
}

function segmented(S, options, prefix, x, y, hover) {
  const ctx = S.ctx;
  ctx.font = font(14, 600);
  const widths = options.map((o) => ctx.measureText(o.label).width + 36);
  const total = widths.reduce((a, b) => a + b, 0) + 4 * (options.length - 1) + 8;
  fill(ctx, C.surface, x, y, total, 45, 22.5);
  let at = x + 4;
  options.forEach((o, i) => {
    const id = `${prefix}:${o.key}`;
    const bg = o.on ? C.text : hover === id ? C.n300 : "transparent";
    if (bg !== "transparent") fill(ctx, bg, at, y + 4, widths[i], 37, 18.5);
    write(ctx, o.label, at + widths[i] / 2, y + 23, font(14, 600), o.on ? C.bg : C.text, "center");
    S.region(id, at, y + 4, widths[i], 37);
    at += widths[i] + 4;
  });
}

function pills(S, options, prefix, x, y, w, hover) {
  const ctx = S.ctx;
  let at = x;
  for (const o of options) {
    const id = `${prefix}:${o.key}`;
    ctx.font = font(14, 600);
    const pw = ctx.measureText(o.label).width + 36;
    if (at + pw > x + w) break;             // no wrapping in a headset; the list is short anyway
    const look = o.on ? { bg: C.text, fg: C.bg } : { bg: C.surface, fg: C.text, hover: C.n300 };
    pillText(S, id, o.label, at, y, font(14, 600), look, hover, 18, 40);
    at += pw + 8;
  }
}

function slider(S, s, x, y, w, hover) {
  const ctx = S.ctx;
  write(ctx, s.label, x, y + 10, font(15, 600), C.text);
  write(ctx, s.valueLabel || "", x + w, y + 10, font(15), C.n700, "right");
  const ty = y + 8 + 20 + 14;
  fill(ctx, C.n300, x, ty - 3, w, 6, 3);
  fill(ctx, C.accent, x, ty - 3, w * s.value, 6, 3);
  ctx.beginPath();
  ctx.arc(x + w * s.value, ty, hover === `slider:${s.key}` ? 12 : 10, 0, Math.PI * 2);
  ctx.fillStyle = C.accent;
  ctx.fill();
  S.region(`slider:${s.key}`, x - 10, ty - 18, w + 20, 36, { kind: "slider", x0: x, x1: x + w });
}

function screenTab(S, m, x0, y, w) {
  const ctx = S.ctx, hover = m.hover;
  y += 4;
  for (const s of m.screen.sliders) {
    slider(S, s, x0, y, w, hover);
    y += 70;
  }
  const recenterStyle = font(15, 400, HEAD);
  ctx.font = recenterStyle;
  const rw = ctx.measureText("Recenter view").width + 40 + 26;
  fill(ctx, hover === "btn:recenter" ? C.accentHover : C.accent, x0, y, rw, 44, 22);
  drawIcon(ctx, "locate", x0 + 20, y + 13, 18, C.bg);
  write(ctx, "Recenter view", x0 + 46, y + 23, recenterStyle, C.bg);
  S.region("btn:recenter", x0, y, rw, 44);
  pillText(S, "btn:resetScreen", m.screen.resetLabel || "Reset size", x0 + rw + 10, y, font(15, 600),
           { bg: hover === "btn:resetScreen" ? C.tint : "transparent", fg: C.text, border: C.line }, hover, 20, 44);
  y += 48;
  if (m.screen.quality) {
    y += 12;
    write(ctx, "Playback", x0, y + 8, font(15, 600), C.text);
    y += 22;
    pills(S, m.screen.quality, "quality", x0, y, w, hover);
    y += 44;
  }
  if (m.screen.hint) {
    y += 10;
    write(ctx, fitted(ctx, m.screen.hint, font(13), w), x0, y + 12, font(13), C.n700);
    y += 24;
  }
  return y;
}

function audioTab(S, m, x0, y, w) {
  const hover = m.hover;
  slider(S, { key: "volume", label: "Volume", valueLabel: `${Math.round(m.volume * 100)}%`, value: m.volume }, x0, y, w, hover);
  y += 70;
  pills(S, [{ key: "", label: m.muted ? "Sound off - press to turn it on" : "Mute", on: m.muted }], "mute", x0, y, w, hover);
  return y + 44;
}

function scenesTab(S, m, x0, y, w) {
  const ctx = S.ctx, hover = m.hover;
  const scenes = m.scenes || [];
  if (!scenes.length) {
    write(ctx, "No scenes marked in this film yet.", x0, y + 12, font(13), C.n700);
    y += 24;
  }
  for (const s of scenes.slice(0, SCENE_ROWS)) {
    fill(ctx, C.surface, x0, y, w, 48, 22);
    // Buttons from the right, then the name and times in what is left.
    let right = x0 + w - 6;
    const button = (id, label, icon = null) => {
      ctx.font = font(13, 600);
      const bw = icon ? 36 : ctx.measureText(label).width + 24;
      right -= bw;
      fill(ctx, hover === id ? C.n300 : C.bg, right, y + 6, bw, 36, 18);
      if (icon) drawIcon(ctx, icon, right + 11, y + 17, 14, C.text);
      else write(ctx, label, right + bw / 2, y + 24.5, font(13, 600), C.text, "center");
      S.region(id, right, y + 6, bw, 36);
      right -= 6;
    };
    button(`scene-del:${s.id}`, "", "x");
    button(`scene-end:${s.id}`, "End here");
    button(`scene-start:${s.id}`, "Start here");
    button(`scene-go:${s.id}`, "Play");
    const room = right - x0 - 22;
    write(ctx, fitted(ctx, s.label, font(15, 700), room), x0 + 16, y + 17, font(15, 700), C.text);
    write(ctx, fitted(ctx, s.times, font(12), room), x0 + 16, y + 34, font(12), C.n700);
    y += 56;
  }
  if (scenes.length) y -= 8;
  if (scenes.length > SCENE_ROWS) {
    write(ctx, `…and ${scenes.length - SCENE_ROWS} more - all of them are listed on the PC`, x0, y + 16, font(13), C.n700);
    y += 26;
  }
  y += 14;
  write(ctx, "Mark scene where one starts, and again where it ends.", x0, y + 8, font(13), C.n700);
  write(ctx, "They are made into passthrough copies on the PC.", x0, y + 26, font(13), C.n700);
  return y + 36;
}

function envTab(S, m, x0, y, w) {
  const ctx = S.ctx, hover = m.hover;
  const cw = (w - 20) / 3;
  m.envs.forEach((o, i) => {
    const cx = x0 + (i % 3) * (cw + 10), cy = y + Math.floor(i / 3) * 125;
    const id = `env:${o.key}`;
    ctx.save();
    shape(ctx, cx, cy, cw, 115, 22);
    ctx.clip();
    ctx.fillStyle = hover === id ? C.n300 : C.surface;
    ctx.fillRect(cx, cy, cw, 115);
    ctx.fillStyle = o.swatch;
    ctx.fillRect(cx, cy, cw, 77);
    ctx.restore();
    write(ctx, o.hint, cx + cw / 2, cy + 40, font(11, 400, "ui-monospace, Menlo, monospace"), o.swatchFg, "center");
    write(ctx, o.label, cx + 15, cy + 96, font(14, 700), C.text);
    if (o.on) {
      shape(ctx, cx + 1.5, cy + 1.5, cw - 3, 112, 20.5);
      ctx.strokeStyle = C.accent;
      ctx.lineWidth = 3;
      ctx.stroke();
    }
    S.region(id, cx, cy, cw, 115);
  });
  y += gridHeight(m.envs.length, 115);
  const pt = m.passthroughOptions;
  if (pt) {
    y += 14;
    write(ctx, "Show your room using", x0, y + 10, font(15, 600), C.text);
    y += 30;
    pills(S, pt.modes, "see", x0, y, w, hover);
    y += 44;
    if (pt.masks.length) {
      write(ctx, "Cut-out", x0, y + 10, font(15, 600), C.text);
      y += 30;
      pills(S, pt.masks, "mask", x0, y, w, hover);
      y += 44;
    }
  }
  return y;
}
