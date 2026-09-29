// Places to watch a flat film in, inside the headset: the Shelf design's Environment choices.
//
// Built from a few shapes and colour gradients rather than photographs or models, so nothing
// is downloaded and the headset spends almost nothing drawing them beside an 8K video. Nothing
// is lit - every colour is simply what it is - which is also why they cost so little.

export const WORLD_CARDS = [
  { key: "void", label: "Void", hint: "warm dark", swatch: "#1c1a18", fg: "#82796a" },
  { key: "cinema", label: "Cinema", hint: "theater seats", swatch: "#3a2418", fg: "#c0b6a5" },
  { key: "lakeside", label: "Lakeside", hint: "dusk lake", swatch: "#3d472b", fg: "#ccdbb2" },
  { key: "observatory", label: "Observatory", hint: "dome + stars", swatch: "#272e1b", fg: "#aebf92" },
  { key: "rooftop", label: "Rooftop", hint: "city night", swatch: "#474238", fg: "#dcd3c4" },
  { key: "passthrough", label: "Passthrough", hint: "your room", swatch: "#a19786", fg: "#2e2b25" },
];

export function buildWorld(THREE, key) {
  const group = new THREE.Group();
  group.name = `world:${key}`;
  const makers = { void: voidWorld, cinema, lakeside, observatory, rooftop };
  (makers[key] || voidWorld)(THREE, group);
  group.traverse((part) => {
    part.layers.enableAll();          // both eyes see the world
    part.renderOrder = -10;           // always behind the film
  });
  return group;
}

export function disposeWorld(group) {
  group.traverse((part) => {
    if (part.geometry) part.geometry.dispose();
    const materials = Array.isArray(part.material) ? part.material : part.material ? [part.material] : [];
    for (const material of materials) {
      if (material.map) material.map.dispose();
      material.dispose();
    }
  });
}

// ---------------------------------------------------------------- pieces

// A sphere seen from inside, coloured by height: stops are [height from -1 to 1, colour].
function sky(THREE, stops, radius = 400) {
  const geometry = new THREE.SphereGeometry(radius, 48, 32);
  const position = geometry.attributes.position;
  const colours = new Float32Array(position.count * 3);
  const colour = new THREE.Color(), a = new THREE.Color(), b = new THREE.Color();
  for (let i = 0; i < position.count; i++) {
    const t = position.getY(i) / radius;
    let k = 0;
    while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
    const [t0, c0] = stops[k], [t1, c1] = stops[k + 1];
    const f = Math.max(0, Math.min(1, (t - t0) / ((t1 - t0) || 1)));
    colour.copy(a.set(c0)).lerp(b.set(c1), f);
    colours.set([colour.r, colour.g, colour.b], i * 3);
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(colours, 3));
  return new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide, depthWrite: false }));
}

function flat(THREE, colour, geometry, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ color: colour }));
  mesh.position.set(x, y, z);
  return mesh;
}

function floorDisc(THREE, colour, radius) {
  const disc = flat(THREE, colour, new THREE.CircleGeometry(radius, 64));
  disc.rotation.x = -Math.PI / 2;
  return disc;
}

// A repeatable random sequence, so a world looks the same every time it is opened.
function seeded(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

// ---------------------------------------------------------------- the worlds

function voidWorld(THREE, group) {
  group.add(sky(THREE, [[-1, "#0d0c0b"], [-0.05, "#161412"], [0.12, "#1c1a18"], [1, "#121110"]]));
  group.add(floorDisc(THREE, "#171513", 30));
  const ring = flat(THREE, "#2e2b25", new THREE.RingGeometry(3.2, 3.26, 96));
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.01;
  group.add(ring);
}

function cinema(THREE, group) {
  // A room around you with the film on its far wall, and rows of seats between.
  const room = new THREE.Mesh(new THREE.BoxGeometry(26, 11, 34),
                              new THREE.MeshBasicMaterial({ color: "#2a1a12", side: THREE.BackSide }));
  room.position.set(0, 5.5, -6);
  group.add(room);
  group.add(flat(THREE, "#1c120c", new THREE.PlaneGeometry(26, 34), 0, 0.01, -6).rotateX(-Math.PI / 2));
  // Curtains either side of the screen wall.
  for (const side of [-1, 1]) group.add(flat(THREE, "#5a2a1e", new THREE.PlaneGeometry(3, 10), side * 11.4, 5, -22.9));
  // Rows of seats, their backs in front of you and behind.
  const seat = new THREE.BoxGeometry(0.62, 1.05, 0.7);
  const rows = [];
  for (let row = -4; row <= 5; row++) {
    if (row === 0) continue;                      // your own row is where you sit
    for (let n = -8; n <= 8; n++) {
      if (Math.abs(n) === 0 && row === 1) continue;
      rows.push([n * 0.72, 0.52 + Math.max(0, row) * 0.28, -row * 1.25 + 0.6]);
    }
  }
  const seats = new THREE.InstancedMesh(seat, new THREE.MeshBasicMaterial({ color: "#4a2218" }), rows.length);
  const place = new THREE.Object3D();
  rows.forEach(([x, y, z], i) => {
    place.position.set(x, y, z);
    place.updateMatrix();
    seats.setMatrixAt(i, place.matrix);
  });
  group.add(seats);
  // Little aisle lights along the floor.
  const lamp = new THREE.CircleGeometry(0.05, 12);
  for (let z = -18; z <= 8; z += 1.6) {
    for (const x of [-6.4, 6.4]) group.add(flat(THREE, "#c67139", lamp, x, 0.02, z).rotateX(-Math.PI / 2));
  }
}

function lakeside(THREE, group) {
  group.add(sky(THREE, [[-1, "#0a0f14"], [-0.02, "#27323c"], [0, "#c67139"], [0.08, "#8a5a58"],
                        [0.3, "#3a3548"], [1, "#161a26"]]));
  // Hills around the far shore, drawn as one jagged dark ring.
  const random = seeded(7);
  const points = [];
  for (let i = 0; i <= 96; i++) {
    const a = (i / 96) * Math.PI * 2;
    const h = 8 + random() * 22 + Math.sin(a * 3) * 6;
    points.push([Math.cos(a) * 300, Math.sin(a) * 300, h]);
  }
  const vertices = [];
  for (let i = 0; i < points.length - 1; i++) {
    const [x0, z0, h0] = points[i], [x1, z1, h1] = points[i + 1];
    vertices.push(x0, -2, z0, x1, -2, z1, x1, h1, z1, x0, -2, z0, x1, h1, z1, x0, h0, z0);
  }
  const hills = new THREE.BufferGeometry();
  hills.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
  group.add(new THREE.Mesh(hills, new THREE.MeshBasicMaterial({ color: "#141a14", side: THREE.DoubleSide })));
  // The water, and a moon with its path across it.
  group.add(flat(THREE, "#1b2530", new THREE.CircleGeometry(290, 64), 0, -1.5, 0).rotateX(-Math.PI / 2));
  const moon = flat(THREE, "#f5ead8", new THREE.CircleGeometry(9, 48), -120, 70, -260);
  moon.lookAt(0, 1.5, 0);
  group.add(moon);
  const glint = flat(THREE, "#6d6a70", new THREE.PlaneGeometry(10, 240), -60, -1.45, -140);
  glint.rotation.set(-Math.PI / 2, 0, Math.atan2(120, 260));
  group.add(glint);
  // A little jetty under you, so there is something to stand on.
  group.add(flat(THREE, "#3a2a1c", new THREE.BoxGeometry(3, 0.2, 7), 0, -0.1, 0.5));
}

function observatory(THREE, group) {
  group.add(sky(THREE, [[-1, "#0d1009"], [0, "#1b2014"], [0.2, "#141a0e"], [1, "#0a0d07"]], 60));
  // Stars on the inside of the dome.
  const random = seeded(42);
  const stars = [];
  for (let i = 0; i < 1800; i++) {
    const u = random(), v = random() * 0.95 + 0.05;
    const a = u * Math.PI * 2, up = Math.asin(v);
    stars.push(Math.cos(up) * Math.cos(a) * 58, Math.sin(up) * 58, Math.cos(up) * Math.sin(a) * 58);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(stars, 3));
  group.add(new THREE.Points(geometry, new THREE.PointsMaterial({ color: "#f5ead8", size: 2, sizeAttenuation: false })));
  // The dome's ribs, and the round floor.
  for (let i = 0; i < 12; i++) {
    const rib = new THREE.Mesh(new THREE.TorusGeometry(59, 0.12, 4, 64, Math.PI),
                               new THREE.MeshBasicMaterial({ color: "#3d472b" }));
    rib.rotation.y = (i / 12) * Math.PI;
    group.add(rib);
  }
  group.add(floorDisc(THREE, "#1b2014", 20));
  for (const r of [4, 8, 14]) {
    const ring = flat(THREE, "#56633f", new THREE.RingGeometry(r, r + 0.06, 128));
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.01;
    group.add(ring);
  }
}

function rooftop(THREE, group) {
  group.add(sky(THREE, [[-1, "#0b0b0e"], [0, "#474238"], [0.06, "#2a2530"], [0.4, "#121320"], [1, "#07080d"]]));
  // Lit windows, drawn once and repeated over every building.
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#15151a";
  ctx.fillRect(0, 0, 64, 64);
  const random = seeded(3);
  for (let y = 4; y < 64; y += 10) {
    for (let x = 4; x < 64; x += 10) {
      ctx.fillStyle = random() > 0.55 ? (random() > 0.5 ? "#f6a06b" : "#e8d9a8") : "#23232a";
      ctx.fillRect(x, y, 5, 6);
    }
  }
  const windows = new THREE.CanvasTexture(canvas);
  windows.colorSpace = THREE.SRGBColorSpace;
  windows.wrapS = windows.wrapT = THREE.RepeatWrapping;
  windows.magFilter = THREE.NearestFilter;
  const buildings = seeded(11);
  for (let i = 0; i < 90; i++) {
    const a = buildings() * Math.PI * 2, r = 70 + buildings() * 160;
    const w = 12 + buildings() * 22, h = 15 + buildings() * 75, d = 12 + buildings() * 20;
    const texture = windows.clone();
    texture.repeat.set(Math.max(1, Math.round(w / 8)), Math.max(1, Math.round(h / 8)));
    texture.needsUpdate = true;
    const tower = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshBasicMaterial({ map: texture }));
    tower.position.set(Math.cos(a) * r, h / 2 - 30, Math.sin(a) * r);
    tower.lookAt(0, h / 2 - 30, 0);
    group.add(tower);
  }
  // The roof under you, and a low wall around its edge.
  group.add(flat(THREE, "#2e2b25", new THREE.BoxGeometry(24, 0.3, 24), 0, -0.15, 0));
  for (const [x, z, w, d] of [[0, -12, 24, 0.3], [0, 12, 24, 0.3], [-12, 0, 0.3, 24], [12, 0, 0.3, 24]]) {
    group.add(flat(THREE, "#474238", new THREE.BoxGeometry(w, 1, d), x, 0.5, z));
  }
}
