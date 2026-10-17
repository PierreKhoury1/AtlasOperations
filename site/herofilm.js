/* The hero film on the Atlas Desks home page: one canvas behind the headline, washed to paper by CSS
   (.hero.eye::before), cycling through three scenes with a crossfade:
     1. vision    — the site's own stock camera clips (video/cam-*.mp4) with real motion boxes: a 48x27 frame diff
                    is clustered into tracks every frame, nothing pre-recorded or drawn by hand
     2. agents    — a run moving through a chain of agents (planner → research / vision → writer → QA → approval → CRM),
                    pulses travelling the edges with what they carry, nodes lighting as work arrives
     3. terminals — two model panes streaming tokens and a desk terminal running a job
   Rendered at half resolution, paused when off-screen or the tab is hidden. Reduced motion: one still frame. */
(function () {
  const host = document.querySelector(".herofilm");
  if (!host) return;
  const rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  host.querySelectorAll("video, img").forEach((v) => v.remove());
  const cv = document.createElement("canvas"); host.appendChild(cv);
  const ctx = cv.getContext("2d", { alpha: true });
  const S = 0.5;
  const MONO = '"IBM Plex Mono", Consolas, monospace';
  const C = { paper: "#ffffff", line: "#c1cad6", bar: "#eef1f6", text: "#10161d", muted: "#5f6b7a", faint: "#b3bdc9",
              blue: "#2d72d2", blueSoft: "#e4eefc", ok: "#1c8f5f", warn: "#a66a00", purple: "#7c5cd6", pink: "#c2409a", ink: "#04070e" };
  // the agent chain and the terminals sit on ink, like the desk recording beside them: white panes vanish under the paper wash
  const T = { paper: "#ffffff", line: "#c1cad6", bar: "#eef1f6", text: "#10161d", muted: "#5f6b7a", faint: "#b3bdc9", ink: "#060a12" };   // white panes on ink: the only pair that survives the paper wash
  let W = 0, H = 0, visible = true;

  function roundRect(c, x, y, w, h, r) {
    if (c.roundRect) { c.roundRect(x, y, w, h, r); return; }
    c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r);
  }
  function card(c, x, y, w, h, r) {
    c.fillStyle = "rgba(0,0,0,.35)"; c.beginPath(); roundRect(c, x, y + 4, w, h, r); c.fill();
    c.fillStyle = T.paper; c.beginPath(); roundRect(c, x, y, w, h, r); c.fill();
    c.strokeStyle = T.line; c.lineWidth = 1; c.beginPath(); roundRect(c, x + .5, y + .5, w - 1, h - 1, r); c.stroke();
  }
  function inkField(now) {
    ctx.fillStyle = T.ink; ctx.fillRect(0, 0, W, H);
    const g = ctx.createRadialGradient(W * (.55 + Math.sin(now / 9000) * .1), H * .5, 0, W * .55, H * .5, W * .7);
    g.addColorStop(0, "rgba(45,114,210,.55)"); g.addColorStop(.5, "rgba(124,92,214,.25)"); g.addColorStop(1, "rgba(6,10,18,0)");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    ctx.strokeStyle = "rgba(140,180,240,.12)"; ctx.lineWidth = 1;
    for (let gx = 0; gx < W; gx += 48 * S) { ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, H); ctx.stroke(); }
    for (let gy = 0; gy < H; gy += 48 * S) { ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke(); }
  }
  function mono(c, px, weight) { c.font = `${weight || 500} ${px}px ${MONO}`; c.textBaseline = "middle"; }

  /* ---------------------------------------------------------------- 1. vision: camera clips + live motion tracks */
  const CAMS = [
    { id: "factory", name: "CAM 03 · FACTORY FLOOR" }, { id: "aisle", name: "CAM 01 · AISLE 4" },
    { id: "stockroom", name: "CAM 05 · STOCK ROOM" }, { id: "produce", name: "CAM 02 · PRODUCE" },
    { id: "meeting", name: "CAM 07 · MEETING ROOM" }, { id: "clinic", name: "CAM 06 · TREATMENT 2" },
  ];
  const GW = 48, GH = 27;
  function mkCam(spec) {
    const v = document.createElement("video");
    v.muted = true; v.loop = true; v.playsInline = true; v.preload = "auto"; v.src = "video/cam-" + spec.id + ".mp4";
    const work = document.createElement("canvas"); work.width = GW; work.height = GH;
    return { spec, v, work, wctx: work.getContext("2d", { willReadFrequently: true }), prev: null, tracks: [], nextId: 1, motion: 0, playing: false, last: 0 };
  }
  const cams = CAMS.map(mkCam);
  let camSet = [0, 1, 2];
  function camPlay(cam) { if (!cam.playing) { cam.playing = true; cam.v.play().catch(() => { cam.playing = false; }); } }
  function camPause(cam) { if (cam.playing) { cam.playing = false; cam.v.pause(); } }

  function track(cam, now) {
    if (cam.v.readyState < 2 || now - cam.last < 70) return;
    cam.last = now;
    cam.wctx.drawImage(cam.v, 0, 0, GW, GH);
    const d = cam.wctx.getImageData(0, 0, GW, GH).data, gray = new Uint8Array(GW * GH);
    for (let i = 0; i < gray.length; i++) gray[i] = (d[i * 4] * 3 + d[i * 4 + 1] * 6 + d[i * 4 + 2]) / 10;
    const prev = cam.prev; cam.prev = gray;
    if (!prev) return;
    const mask = new Uint8Array(GW * GH); let hot = 0;
    for (let i = 0; i < gray.length; i++) if (Math.abs(gray[i] - prev[i]) > 26) { mask[i] = 1; hot++; }
    cam.motion = cam.motion * .8 + (hot / gray.length) * .2;
    // connected components (8-neighbour flood) → boxes
    const seen = new Uint8Array(GW * GH), boxes = [], stack = [];
    for (let i = 0; i < mask.length; i++) {
      if (!mask[i] || seen[i]) continue;
      let x0 = GW, y0 = GH, x1 = 0, y1 = 0, n = 0; stack.push(i); seen[i] = 1;
      while (stack.length) {
        const k = stack.pop(), x = k % GW, y = (k / GW) | 0; n++;
        if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          const nx = x + dx, ny = y + dy; if (nx < 0 || ny < 0 || nx >= GW || ny >= GH) continue;
          const j = ny * GW + nx; if (mask[j] && !seen[j]) { seen[j] = 1; stack.push(j); }
        }
      }
      if (n >= 5) boxes.push({ x: x0 / GW, y: y0 / GH, w: (x1 - x0 + 1) / GW, h: (y1 - y0 + 1) / GH, n });
    }
    boxes.sort((a, b) => b.n - a.n); boxes.length = Math.min(boxes.length, 4);
    // match to tracks by centre distance, smooth, age out
    boxes.forEach((b) => {
      const cx = b.x + b.w / 2, cy = b.y + b.h / 2; let best = null, bd = .22;
      cam.tracks.forEach((t) => { const dd = Math.hypot(t.x + t.w / 2 - cx, t.y + t.h / 2 - cy); if (dd < bd && !t.hit) { bd = dd; best = t; } });
      if (best) { best.x += (b.x - best.x) * .35; best.y += (b.y - best.y) * .35; best.w += (b.w - best.w) * .35; best.h += (b.h - best.h) * .35; best.seen = now; best.hit = true; best.age++; best.score = Math.min(.99, .5 + b.n / 60); }
      else cam.tracks.push({ ...b, id: cam.nextId++, seen: now, hit: true, age: 0, score: Math.min(.99, .5 + b.n / 60) });
    });
    cam.tracks = cam.tracks.filter((t) => now - t.seen < 900); cam.tracks.forEach((t) => { t.hit = false; });
  }

  function drawCam(cam, x, y, w, h, now, big) {
    const s = S;
    ctx.save(); ctx.beginPath(); roundRect(ctx, x, y, w, h, 4 * s); ctx.clip();
    ctx.fillStyle = C.ink; ctx.fillRect(x, y, w, h);
    const v = cam.v;
    if (v.readyState >= 2 && v.videoWidth) {
      const vr = v.videoWidth / v.videoHeight, r = w / h; let dw = w, dh = h;
      if (vr > r) dw = h * vr; else dh = w / vr;
      ctx.drawImage(v, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
    }
    // scan grid + corner brackets
    ctx.strokeStyle = "rgba(255,255,255,.07)"; ctx.lineWidth = 1;
    for (let k = 1; k < 3; k++) { ctx.beginPath(); ctx.moveTo(x + w * k / 3, y); ctx.lineTo(x + w * k / 3, y + h); ctx.moveTo(x, y + h * k / 3); ctx.lineTo(x + w, y + h * k / 3); ctx.stroke(); }
    const L = 14 * s, m = 8 * s; ctx.strokeStyle = "rgba(255,255,255,.55)"; ctx.lineWidth = 1.5 * s;
    [[x + m, y + m, 1, 1], [x + w - m, y + m, -1, 1], [x + m, y + h - m, 1, -1], [x + w - m, y + h - m, -1, -1]].forEach(([cx, cy, dx, dy]) => {
      ctx.beginPath(); ctx.moveTo(cx, cy + dy * L); ctx.lineTo(cx, cy); ctx.lineTo(cx + dx * L, cy); ctx.stroke();
    });
    // tracks
    cam.tracks.filter((t) => t.age >= 2).forEach((t) => {
      const bx = x + t.x * w, by = y + t.y * h, bw = Math.max(18 * s, t.w * w), bh = Math.max(18 * s, t.h * h);
      ctx.strokeStyle = C.blue; ctx.lineWidth = 1.6 * s; ctx.strokeRect(bx, by, bw, bh);
      ctx.fillStyle = "rgba(45,114,210,.12)"; ctx.fillRect(bx, by, bw, bh);
      mono(ctx, 9.5 * s, 600); const label = `motion ${t.id}  ${t.score.toFixed(2)}`; const tw = ctx.measureText(label).width + 8 * s;
      ctx.fillStyle = C.blue; ctx.fillRect(bx, by - 13 * s, tw, 13 * s); ctx.fillStyle = "#fff"; ctx.fillText(label, bx + 4 * s, by - 6.5 * s);
    });
    // HUD
    mono(ctx, (big ? 10.5 : 9) * s, 500); ctx.fillStyle = "rgba(255,255,255,.9)";
    ctx.fillText(cam.spec.name, x + 14 * s, y + 24 * s);
    ctx.fillStyle = "rgba(255,255,255,.65)";
    const tk = new Date(now + 1760690000000); const ts = tk.toISOString().slice(11, 19);
    ctx.fillText(`${ts}  ·  diff ${GW}×${GH}  ·  motion ${(cam.motion * 100).toFixed(1)}%  ·  tracks ${cam.tracks.filter((t) => t.age >= 2).length}`, x + 14 * s, y + h - 16 * s);
    ctx.fillStyle = cam.motion > .01 ? C.ok : "rgba(255,255,255,.4)"; ctx.beginPath(); ctx.arc(x + w - 16 * s, y + 24 * s, 3.5 * s, 0, 6.28); ctx.fill();
    ctx.restore();
  }

  const vision = {
    enter() { camSet = camSet.map((i) => (i + 3) % cams.length); camSet.forEach((i) => camPlay(cams[i])); },
    leave() { cams.forEach(camPause); },
    draw(now) {
      camSet.forEach((i) => track(cams[i], now));
      const s = S, pad = 16 * s, gap = 12 * s;
      const bw = W * .58 - pad, bh = H - pad * 2;
      const [a, b, c] = camSet.map((i) => cams[i]);
      drawCam(a, pad + Math.sin(now / 6000) * 4 * s, pad, bw, bh, now, true);
      const rx = pad + bw + gap, rw = W - rx - pad * .5, rh = (bh - gap) / 2;
      drawCam(b, rx, pad, rw, rh, now, false);
      drawCam(c, rx, pad + rh + gap, rw, rh, now, false);
    },
  };

  /* ---------------------------------------------------------------- 2. agents: a run through the chain */
  const NODES = [
    { id: "inbox", t: "Inbox", sub: "12 new · 3 bulk skipped", x: .07, y: .30 },
    { id: "cams", t: "Cameras", sub: "7 feeds · motion", x: .07, y: .68 },
    { id: "plan", t: "Planner", sub: "claude · plan 6 steps", x: .27, y: .49, big: true },
    { id: "research", t: "Research", sub: "companies house · web", x: .47, y: .18 },
    { id: "vision", t: "Vision", sub: "yolov8n + vlm", x: .47, y: .49 },
    { id: "writer", t: "Writer", sub: "draft v2 · 87 words", x: .47, y: .80 },
    { id: "qa", t: "QA", sub: "facts · tone · policy", x: .67, y: .49 },
    { id: "approve", t: "Approval", sub: "waiting: owner", x: .85, y: .30 },
    { id: "crm", t: "CRM · send", sub: "stage → qualified", x: .85, y: .68 },
  ];
  const EDGES = [["inbox", "plan"], ["cams", "plan"], ["plan", "research"], ["plan", "vision"], ["plan", "writer"], ["research", "writer"], ["vision", "writer"], ["writer", "qa"], ["qa", "writer"], ["qa", "approve"], ["approve", "crm"]];
  const RUN = [   // [edge, label, delay after the previous pulse lands]
    [["inbox", "plan"], "lead #4812", 0], [["cams", "plan"], "frame 41 · person=1", 200], [["plan", "research"], "task: fit", 700], [["plan", "vision"], "task: look", 150],
    [["plan", "writer"], "brief", 150], [["research", "writer"], "gas-safe ✓ 24/7 ✓", 1300], [["vision", "writer"], "yard empty", 200],
    [["writer", "qa"], "draft v1 · 218 w", 1500], [["qa", "writer"], "too long · no md", 900], [["writer", "qa"], "draft v2 · 87 w", 1400],
    [["qa", "approve"], "facts ✓ tone ✓", 900], [["approve", "crm"], "approved 09:41", 2600],
  ];
  const N = Object.fromEntries(NODES.map((n) => [n.id, n]));
  const pulses = []; let runStep = 0, runWait = 600, nodeHeat = Object.fromEntries(NODES.map((n) => [n.id, 0]));
  function edgePath(a, b) { const x0 = a.x * W, y0 = a.y * H, x1 = b.x * W, y1 = b.y * H; const mx = (x0 + x1) / 2; return [x0, y0, mx, y0, mx, y1, x1, y1]; }
  function bez(p, t) { const u = 1 - t; const f = (a, b, c, d) => u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * d; return [f(p[0], p[2], p[4], p[6]), f(p[1], p[3], p[5], p[7])]; }
  const agents = {
    enter() {}, leave() {},
    step(dt) {
      runWait -= dt;
      if (runWait <= 0 && runStep < RUN.length) { const [e, label] = RUN[runStep]; pulses.push({ a: N[e[0]], b: N[e[1]], label, t: 0, dur: 1100 + Math.random() * 300 }); runWait = RUN[runStep][2] + 1100; runStep++; nodeHeat[e[0]] = 1; }
      if (runStep >= RUN.length && runWait <= -2500) { runStep = 0; runWait = 800; }
      pulses.forEach((p) => { p.t += dt / p.dur; if (p.t >= 1 && !p.hit) { p.hit = true; nodeHeat[p.b.id] = 1; } });
      for (let i = pulses.length - 1; i >= 0; i--) if (pulses[i].t > 1.25) pulses.splice(i, 1);
      Object.keys(nodeHeat).forEach((k) => { nodeHeat[k] = Math.max(0, nodeHeat[k] - dt / 1600); });
    },
    draw(now) {
      const s = S;
      inkField(now);
      EDGES.forEach(([a, b]) => {
        const p = edgePath(N[a], N[b]); ctx.strokeStyle = "rgba(156,195,255,.45)"; ctx.lineWidth = 1.4 * s; ctx.setLineDash([]);
        ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.bezierCurveTo(p[2], p[3], p[4], p[5], p[6], p[7]); ctx.stroke();
      });
      pulses.forEach((p) => {
        const path = edgePath(p.a, p.b), t = Math.min(1, p.t), [x, y] = bez(path, t);
        ctx.strokeStyle = "#9cc3ff"; ctx.lineWidth = 2.4 * s; ctx.beginPath();
        for (let k = 0; k <= 24; k++) { const [px, py] = bez(path, t * k / 24); k ? ctx.lineTo(px, py) : ctx.moveTo(px, py); }
        ctx.globalAlpha = p.t > 1 ? 1 - (p.t - 1) * 4 : 1; ctx.stroke();
        ctx.fillStyle = "#ffffff"; ctx.beginPath(); ctx.arc(x, y, 5 * s, 0, 6.28); ctx.fill();
        ctx.fillStyle = "rgba(156,195,255,.35)"; ctx.beginPath(); ctx.arc(x, y, 12 * s, 0, 6.28); ctx.fill();
        mono(ctx, 10 * s, 600); const tw = ctx.measureText(p.label).width + 10 * s;
        ctx.fillStyle = C.paper; ctx.fillRect(x + 10 * s, y - 20 * s, tw, 15 * s); ctx.strokeStyle = C.blue; ctx.lineWidth = 1; ctx.strokeRect(x + 10 * s, y - 20 * s, tw, 15 * s);
        ctx.fillStyle = C.blue; ctx.fillText(p.label, x + 15 * s, y - 12.5 * s);
        ctx.globalAlpha = 1;
      });
      NODES.forEach((n) => {
        const w = (n.big ? 150 : 128) * s, h = (n.big ? 58 : 50) * s, x = n.x * W - w / 2, y = n.y * H - h / 2, heat = nodeHeat[n.id];
        if (heat > 0) { ctx.fillStyle = `rgba(156,195,255,${heat * .45})`; ctx.beginPath(); roundRect(ctx, x - 8 * s * heat, y - 8 * s * heat, w + 16 * s * heat, h + 16 * s * heat, 8 * s); ctx.fill(); }
        card(ctx, x, y, w, h, 5 * s);
        ctx.fillStyle = heat > .05 ? C.blue : T.line; ctx.fillRect(x, y, 3 * s, h);
        ctx.font = `600 ${13 * s}px Inter, system-ui, sans-serif`; ctx.textBaseline = "middle"; ctx.fillStyle = T.text; ctx.fillText(n.t, x + 12 * s, y + 18 * s);
        mono(ctx, 9 * s, 500); ctx.fillStyle = heat > .3 ? C.blue : T.muted; ctx.fillText(heat > .3 ? "working…" : n.sub, x + 12 * s, y + h - 15 * s);
        ctx.fillStyle = heat > .05 ? C.ok : T.faint; ctx.beginPath(); ctx.arc(x + w - 11 * s, y + 13 * s, 3 * s, 0, 6.28); ctx.fill();
      });
      mono(ctx, 10 * s, 500); ctx.fillStyle = "rgba(255,255,255,.75)"; ctx.fillText(`run 4812  ·  step ${Math.min(runStep, RUN.length)}/${RUN.length}  ·  nothing sent without approval`, 16 * s, H - 18 * s);
    },
  };

  /* ---------------------------------------------------------------- 3. terminals: token streams + a desk job */
  const TOKENS = [
    "The lead is a two-person plumbing firm in Pimlico. Their site lists emergency call-outs but no gas-safety page, so the reply should mention the check explicitly. Tone: direct, no prices in writing. Booking window tomorrow 08:00–10:00 is free.",
    "Seven tenants are more than seven days late. Two are on agreed payment plans and are skipped. For the other five I drafted firm, polite reminders with the amount, the date and the bank details; no late fees, the agreement does not allow them.",
    "A silver hatchback is parked beside the yellow generator. The yard is otherwise empty; the gate on the right is open. No people visible. Since the last frame nothing has moved.",
    "Tenant at Flat 2 reports the boiler has been off since Tuesday. The tenancy says heating repairs within 24 hours. I drafted a message to the gas engineer for Thursday morning and a holding reply to the tenant; both wait for your approval.",
    "QA note: the draft claims a 30-minute response time, which is not in the policy. Replaced with 'same day'. Removed markdown. Word count 87.",
  ];
  const SCRIPT = [
    [["$ ", "b"], ["atlas desk \"Hollis & Grey lettings\" --run \"arrears sweep\"", "t"]],
    [["[inbox]    ", "b"], ["12 new · 3 machine mail skipped · 9 to the desk", "t"]],
    [["[arrears]  ", "k"], ["7 tenants > 7 days late · 2 on a plan → skip", "t"]],
    [["[writer]   ", "k"], ["5 reminders drafted · firm, polite · no fees", "t"]],
    [["[qa]       ", "y"], ["facts ✓  tone ✓  policy ✓  edits: 1", "t"]],
    [["[approval] ", "w"], ["5 messages waiting · manager@hollis-grey", "w"]],
    [["[session]  ", "m"], ["claude session · account 14 · $0.04 this run", "m"]],
    [["✓ ", "g"], ["run 4819 done · 11 steps · 2 m 06 s", "g"]],
    [["$ ", "b"], ["atlas cameras yard --ask \"anyone at the gate?\"", "t"]],
    [["[vision]   ", "b"], ["frame 41 · motion .18 · person=0 · car=1", "t"]],
    [["{\"answer\": ", "m"], ["\"No. One silver hatchback by the generator.\"}", "t"]],
    [["[schedule] ", "b"], ["08:00 weekdays (Europe/London) · next Mon", "t"]],
  ];
  const COL = { t: T.text, m: T.muted, b: C.blue, w: C.warn, k: C.purple, y: C.pink, g: C.ok };
  const toks = [0, 2].map((i) => ({ text: TOKENS[i], i, pos: 0, wait: 0, tps: 38, done: 0 }));
  const term = { lines: [], cur: null, idx: 0, char: 0, wait: 0, blink: 0 };
  function stepTok(t, dt) {
    if (t.wait > 0) { t.wait -= dt; return; }
    if (t.pos >= t.text.length) { t.done += dt; if (t.done > 2400) { t.i += 2; t.text = TOKENS[t.i % TOKENS.length]; t.pos = 0; t.done = 0; } return; }
    const n = 2 + Math.floor(Math.random() * 6); t.pos = Math.min(t.text.length, t.pos + n);
    t.tps = Math.round(34 + Math.sin(performance.now() / 900) * 9 + Math.random() * 4); t.wait = 1000 / t.tps * n / 3.2;
  }
  function stepTerm(t, dt) {
    t.blink += dt; if (t.wait > 0) { t.wait -= dt; return; }
    if (!t.cur) { t.cur = { parts: SCRIPT[t.idx], shown: 0 }; t.idx = (t.idx + 1) % SCRIPT.length; t.char = 0; }
    const full = t.cur.parts.map((p) => p[0]).join(""), isCmd = t.cur.parts[0][0] === "$ ";
    t.char += dt * (isCmd ? 0.03 : 0.17); t.cur.shown = Math.min(full.length, Math.floor(t.char));
    if (t.cur.shown >= full.length) { t.lines.push(t.cur.parts); t.cur = null; if (t.lines.length > 12) t.lines.shift(); t.wait = isCmd ? 150 : 160 + Math.random() * 500; }
  }
  function paneHead(x, y, w, title) {
    const s = S, bh = 22 * s; ctx.fillStyle = T.bar; ctx.fillRect(x + 1, y + 1, w - 2, bh); ctx.fillStyle = T.line; ctx.fillRect(x + 1, y + bh, w - 2, 1);
    for (let k = 0; k < 3; k++) { ctx.fillStyle = T.faint; ctx.beginPath(); ctx.arc(x + (12 + k * 12) * s, y + bh / 2, 3.2 * s, 0, 6.28); ctx.fill(); }
    mono(ctx, 9.5 * s, 500); ctx.fillStyle = T.muted; ctx.fillText(title.toUpperCase(), x + 50 * s, y + bh / 2); return bh;
  }
  function drawTok(t, x, y, w, h, title) {
    const s = S; card(ctx, x, y, w, h, 4 * s); const bh = paneHead(x, y, w, title);
    const fs = 14 * s, lh = 20 * s, pad = 14 * s; let cy = y + bh + pad + fs * .5;
    ctx.save(); ctx.beginPath(); ctx.rect(x, y + bh, w, h - bh); ctx.clip(); mono(ctx, fs, 500);
    ctx.fillStyle = T.muted; ctx.fillText(`stream  ·  ${t.tps} tok/s  ·  temp 0.1  ·  ${t.pos < t.text.length ? "generating" : "done"}`, x + pad, cy); cy += lh * 1.2;
    const words = t.text.slice(0, t.pos).split(" "), maxW = w - pad * 2; let line = "", lines = [];
    words.forEach((wd) => { const test = line ? line + " " + wd : wd; if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = wd; } else line = test; });
    lines.push(line);
    const maxLines = Math.floor((h - bh - pad * 2) / lh) - 2;
    lines.slice(-maxLines).forEach((ln, li, arr) => {
      ctx.fillStyle = T.text; ctx.fillText(ln, x + pad, cy);
      if (li === arr.length - 1 && t.pos < t.text.length) {
        const tail = ln.slice(-5), tw = ctx.measureText(tail).width, lw = ctx.measureText(ln).width;
        ctx.fillStyle = C.blueSoft; ctx.fillRect(x + pad + lw - tw, cy - fs * .6, tw + 2 * s, fs * 1.2); ctx.fillStyle = C.blue; ctx.fillText(tail, x + pad + lw - tw, cy);
      }
      cy += lh;
    });
    ctx.restore();
  }
  function drawTerm(x, y, w, h, title) {
    const s = S; card(ctx, x, y, w, h, 4 * s); const bh = paneHead(x, y, w, title);
    const fs = 13.5 * s, lh = 19 * s, pad = 12 * s; let cy = y + bh + pad + fs * .5; mono(ctx, fs, 500);
    ctx.save(); ctx.beginPath(); ctx.rect(x, y + bh, w, h - bh); ctx.clip();
    const maxLines = Math.floor((h - bh - pad * 2) / lh), all = term.lines.slice(); if (term.cur) all.push(term.cur.parts);
    const vis = all.slice(-maxLines);
    vis.forEach((parts, li) => {
      let cx = x + pad; const isCur = term.cur && li === vis.length - 1; let budget = isCur ? term.cur.shown : 1e9;
      parts.forEach((pt) => { const str = pt[0].slice(0, Math.max(0, budget)); budget -= pt[0].length; ctx.fillStyle = COL[pt[1]] || T.text; ctx.fillText(str, cx, cy); cx += ctx.measureText(str).width; });
      if (isCur && Math.floor(term.blink / 500) % 2 === 0) { ctx.fillStyle = C.blue; ctx.fillRect(cx + 2 * s, cy - fs * .55, 6 * s, fs * 1.1); }
      cy += lh;
    });
    ctx.restore();
  }
  const terminals = {
    enter() {}, leave() {},
    step(dt) { toks.forEach((t) => stepTok(t, dt)); stepTerm(term, dt); },
    draw(now) {
      const s = S;
      inkField(now);
      const fl = (k) => Math.sin(now / 4200 + k) * 5 * s;
      // three panes staggered on the ink, never the whole field: the model panes stream, the desk terminal runs
      drawTok(toks[0], W * .04, H * .08 + fl(0), W * .34, H * .5, "claude · streaming");
      drawTerm(W * .30, H * .50 + fl(2), W * .40, H * .44, "atlas · run 4819");
      drawTok(toks[1], W * .66, H * .12 + fl(4), W * .31, H * .46, "claude · drafting reply");
    },
  };

  /* ---------------------------------------------------------------- the cycle */
  const SCENES = [vision, agents, terminals];
  const HOLD = 11000, FADE = 1600;
  const want = ["vision", "agents", "terminals"].indexOf((location.hash.match(/film=(\w+)/) || [])[1]);   // #film=agents: start there (for checking)
  let si = want < 0 ? 0 : want, sinceT = 0, fading = false;
  function size() { const r = host.getBoundingClientRect(); W = cv.width = Math.round(Math.max(320, r.width) * S); H = cv.height = Math.round(Math.max(240, r.height) * S); }
  size(); new ResizeObserver(size).observe(host);
  new IntersectionObserver((es) => es.forEach((e) => { visible = e.isIntersecting; }), { rootMargin: "100px" }).observe(host);

  if (rm) { agents.step(5000); agents.draw(0); return; }
  SCENES[si].enter();
  let last = performance.now();
  function frame(now) {
    const dt = Math.min(80, now - last); last = now;
    if (!document.hidden && visible) {
      sinceT += dt;
      const cur = SCENES[si], nxt = SCENES[(si + 1) % SCENES.length];
      if (sinceT > HOLD && !fading) { fading = true; nxt.enter(); }
      if (cur.step) cur.step(dt); if (fading && nxt.step) nxt.step(dt);
      ctx.clearRect(0, 0, W, H);
      ctx.globalAlpha = 1; cur.draw(now);
      if (fading) {
        const f = Math.min(1, (sinceT - HOLD) / FADE);
        ctx.globalAlpha = f; nxt.draw(now); ctx.globalAlpha = 1;
        if (f >= 1) { cur.leave(); si = (si + 1) % SCENES.length; sinceT = 0; fading = false; }
      }
    } else if (document.hidden) { cams.forEach(camPause); SCENES[si].enter && visible && 0; }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && SCENES[si] === vision) camSet.forEach((i) => camPlay(cams[i])); });
  window.HeroFilm = { cams, pulses, next() { sinceT = HOLD; } };
})();
