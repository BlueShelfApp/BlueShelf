// The player's icons: Lucide (ISC licence), drawn at stroke width 2.75 as the Shelf design
// asks. Each is a list of [path, filled] on a 24 by 24 grid, so the same shapes are drawn as
// SVG on the page and straight onto the headset panel's canvas.

const RECT = (x, y, w, h, r) =>
  `M${x + r} ${y}h${w - 2 * r}a${r} ${r} 0 0 1 ${r} ${r}v${h - 2 * r}a${r} ${r} 0 0 1 -${r} ${r}` +
  `h-${w - 2 * r}a${r} ${r} 0 0 1 -${r} -${r}v-${h - 2 * r}a${r} ${r} 0 0 1 ${r} -${r}z`;
const CIRCLE = (cx, cy, r) => `M${cx + r} ${cy}a${r} ${r} 0 1 1 -${2 * r} 0a${r} ${r} 0 1 1 ${2 * r} 0z`;

export const ICONS = {
  play: [["M6 3L20 12L6 21Z", true]],
  pause: [[RECT(14, 4, 4, 16, 1), true], [RECT(6, 4, 4, 16, 1), true]],
  next: [["M5 4L15 12L5 20Z", true], ["M19 5V19", false]],
  prev: [["M19 20L9 12L19 4Z", true], ["M5 19V5", false]],
  ccw: [["M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8", false], ["M3 3v5h5", false]],
  cw: [["M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8", false], ["M21 3v5h-5", false]],
  volume: [["M11 5L6 9H2V15H6L11 19Z", false], ["M15.54 8.46a5 5 0 0 1 0 7.07", false],
           ["M19.07 4.93a10 10 0 0 1 0 14.14", false]],
  mute: [["M11 5L6 9H2V15H6L11 19Z", false], ["M22 9l-6 6", false], ["M16 9l6 6", false]],
  captions: [[RECT(3, 5, 18, 14, 2), false], ["M7 15h4M15 15h2M7 11h2M13 11h4", false]],
  sliders: [["M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4", false]],
  eye: [["M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z", false], [CIRCLE(12, 12, 3), false]],
  scale: [["M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7", false]],
  mountain: [["m8 3 4 8 5-5 5 15H2L8 3z", false]],
  locate: [["M2 12h3M19 12h3M12 2v3M12 19v3", false], [CIRCLE(12, 12, 7), false]],
  box: [["M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z", false],
        ["m3.3 7 8.7 5 8.7-5M12 22V12", false]],
  globe: [[CIRCLE(12, 12, 10), false],
          ["M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z", false]],
  down: [["m6 9 6 6 6-6", false]],
  up: [["m18 15-6-6-6 6", false]],
  x: [["M18 6 6 18M6 6l12 12", false]],
  // Not in the design, for what BlueShelf does that it does not show.
  heart: [["M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z", false]],
  heartFull: [["M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z", true]],
  trash: [["M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2M10 11v6M14 11v6", false]],
  goggles: [["M20 6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-4a2 2 0 0 1-1.6-.8l-1.6-2.13a1 1 0 0 0-1.6 0L9.6 17.2A2 2 0 0 1 8 18H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z", false]],
  leave: [["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9", false]],
  expand: [["M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3", false]],
  back: [["m12 19-7-7 7-7M19 12H5", false]],
  star: [["M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z", false]],
  starFull: [["M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z", true]],
};

const STROKE = 2.75;

// For the page: inline SVG that takes its colour from the text around it.
export function iconSvg(name) {
  const parts = (ICONS[name] || []).map(([d, filled]) =>
    `<path d="${d}"${filled ? ' fill="currentColor"' : ""}/>`).join("");
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${STROKE}" ` +
         `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${parts}</svg>`;
}

// For the headset: the same shapes onto a canvas, at (x, y) and size pixels across.
const cache = new Map();
export function drawIcon(ctx, name, x, y, size, colour) {
  let paths = cache.get(name);
  if (!paths) {
    paths = (ICONS[name] || []).map(([d, filled]) => [new Path2D(d), filled]);
    cache.set(name, paths);
  }
  ctx.save();
  ctx.translate(x, y);
  ctx.scale(size / 24, size / 24);
  ctx.strokeStyle = ctx.fillStyle = colour;
  ctx.lineWidth = STROKE;
  ctx.lineCap = ctx.lineJoin = "round";
  for (const [path, filled] of paths) {
    if (filled) ctx.fill(path);
    ctx.stroke(path);
  }
  ctx.restore();
}
