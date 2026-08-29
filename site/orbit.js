/* Orbit — the Hermes desk, drawn live as a background.
   One orchestrator (Hermes) in the middle, specialists around it, an approval gate
   where a human ticks the work, and a customer on the far side. Packets flow through
   the loop forever: lead → Hermes → specialists (parallel) → Hermes → approval → sent.

   Usage:  <canvas class="orbit" data-orbit='{"alpha":1}'></canvas>
   API:    Orbit.setStage(n)   n = -1 (free), 0..4 (focus a stage: camera moves there)
           Orbit.setScroll(y)  parallax hook (cinema.js feeds it)
   Respects prefers-reduced-motion (single still frame). */
(function () {
  const AG = {
    hermes:   { c: "#c084fc", label: "HERMES", sub: "orchestrator" },
    research: { c: "#60a5fa", label: "RESEARCH" },
    writer:   { c: "#f472b6", label: "WRITER" },
    qa:       { c: "#fbbf24", label: "QA" },
    crm:      { c: "#34d399", label: "CRM" },
    lead:     { c: "#a78bfa", label: "LEAD", sub: "form · email · whatsapp" },
    approve:  { c: "#ffffff", label: "YOU", sub: "approve · edit · reject" },
    client:   { c: "#3ddc97", label: "SENT", sub: "email · crm · follow-up" },
  };
  // scene coordinates (units of ring radius)
  const N = {
    lead:     { x: -2.45, y: 0.15, r: 0.16 },
    hermes:   { x: 0.0,   y: 0.0,  r: 0.26 },
    research: { x: -1.25, y: -0.9, r: 0.14 },
    writer:   { x: -0.4,  y: -1.4, r: 0.14 },
    qa:       { x: 0.55,  y: -1.4, r: 0.14 },
    crm:      { x: 1.3,   y: -0.9, r: 0.14 },
    approve:  { x: 1.95,  y: 0.55, r: 0.2 },
    client:   { x: 3.4,   y: 0.65, r: 0.16 },
  };
  const SPEC = ["research", "writer", "qa", "crm"];
  const EDGES = [
    ["lead", "hermes"], ["hermes", "research"], ["hermes", "writer"], ["hermes", "qa"], ["hermes", "crm"],
    ["hermes", "approve"], ["approve", "client"],
  ];
  // which nodes each stage lights up
  const STAGE_NODES = [
    ["lead", "hermes"],
    ["hermes", "research", "writer", "qa", "crm"],
    ["research", "writer", "qa", "crm", "hermes"],
    ["hermes", "approve"],
    ["approve", "client"],
  ];
  const STAGE_CAM = [
    { x: -2.0, y: 0.05, z: 1.2 },
    { x: 0.0, y: -0.6, z: 1.2 },
    { x: 0.0, y: -0.55, z: 1.2 },
    { x: 0.55, y: 0.25, z: 1.35 },
    { x: 2.4, y: 0.6, z: 1.35 },
  ];
  const LOOP = 11; // seconds per full cycle

  function hexA(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }
  const ease = (t) => (t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  function mount(canvas, opts) {
    const o = Object.assign({ alpha: 1, stars: 110, maxDpr: 1.5, zoom: 1 }, opts || {});
    const ctx = canvas.getContext("2d");
    const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let w = 1, h = 1, dpr = 1, raf = 0, t0 = performance.now(), last = t0;
    let stage = -1, scrollY = 0, mx = 0, my = 0, par = 0, shift = 0;
    const cam = { x: 0.4, y: 0, z: 1 };            // current
    const want = { x: 0.4, y: 0, z: 1 };           // target
    const heat = {};                                // edge glow, decays
    const nodeHeat = {};                            // node pulse, decays
    const stars = [];
    for (let i = 0; i < o.stars; i++) stars.push({ x: Math.random(), y: Math.random(), z: 0.3 + Math.random() * 0.7, s: Math.random() * 1.4 + 0.3 });

    function size() {
      dpr = Math.min(o.maxDpr, window.devicePixelRatio || 1);
      w = canvas.clientWidth || window.innerWidth;
      h = canvas.clientHeight || window.innerHeight;
      canvas.width = (w * dpr) | 0;
      canvas.height = (h * dpr) | 0;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    size();
    window.addEventListener("resize", size);
    window.addEventListener("pointermove", (e) => { mx = e.clientX / w - 0.5; my = e.clientY / h - 0.5; }, { passive: true });

    function unit() { return Math.min(w / 6.6, h / 3.9); }
    function P(n) { // scene → screen
      const u = unit() * cam.z;
      const px = w * 0.5 + shift + (n.x - cam.x) * u + mx * -18;
      const py = h * 0.52 + (n.y - cam.y) * u + my * -14 + par;
      return [px, py];
    }

    // ---- packet schedule: returns list of {from,to,p,color} for time t in loop
    function packets(tt) {
      const out = [];
      const seg = (a, b, from, to, c) => {
        if (tt >= a && tt < b) out.push({ from, to, p: ease((tt - a) / (b - a)), c });
      };
      seg(0.0, 1.3, "lead", "hermes", AG.lead.c);
      for (const s of SPEC) seg(1.8, 2.9, "hermes", s, AG[s].c);
      // 2.9–4.3 specialists working (pulse), then return
      for (const s of SPEC) seg(4.3, 5.4, s, "hermes", AG[s].c);
      seg(5.9, 6.9, "hermes", "approve", AG.hermes.c);
      // 6.9–8.6 waiting for human
      seg(8.6, 9.8, "approve", "client", AG.approve.c);
      return out;
    }
    function loopStage(tt) {
      if (tt < 1.6) return 0;
      if (tt < 4.3) return 1;
      if (tt < 5.9) return 2;
      if (tt < 8.6) return 3;
      return 4;
    }

    function drawEdge(a, b, alpha, col, width) {
      const [x1, y1] = P(N[a]), [x2, y2] = P(N[b]);
      const cx = (x1 + x2) / 2 + (y2 - y1) * 0.18, cy = (y1 + y2) / 2 - (x2 - x1) * 0.18;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.quadraticCurveTo(cx, cy, x2, y2);
      ctx.strokeStyle = hexA(col, alpha);
      ctx.lineWidth = width;
      ctx.stroke();
      return [x1, y1, cx, cy, x2, y2];
    }
    function onCurve(c, p) {
      const [x1, y1, cx, cy, x2, y2] = c, q = 1 - p;
      return [q * q * x1 + 2 * q * p * cx + p * p * x2, q * q * y1 + 2 * q * p * cy + p * p * y2];
    }
    function glow(x, y, r, col, a) {
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, hexA(col, a));
      g.addColorStop(1, hexA(col, 0));
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283); ctx.fill();
    }

    function frame(now) {
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      const T = (now - t0) / 1000, tt = T % LOOP;
      const live = stage < 0 ? loopStage(tt) : stage;   // stage shown
      const A = o.alpha;
      // camera
      const tg = stage < 0 ? { x: 0.4, y: 0, z: o.zoom } : STAGE_CAM[stage];
      want.x = tg.x; want.y = tg.y; want.z = tg.z;
      const k = 1 - Math.pow(0.02, dt);
      cam.x += (want.x - cam.x) * k; cam.y += (want.y - cam.y) * k; cam.z += (want.z - cam.z) * k;
      par += ((stage < 0 ? -scrollY * 0.06 : 0) - par) * k;
      shift += ((stage >= 0 && w > 960 ? w * 0.14 : 0) - shift) * k;

      ctx.clearRect(0, 0, w, h);
      const u = unit() * cam.z;

      // stars (depth parallax)
      for (const s of stars) {
        const sx = ((s.x + T * 0.004 * s.z) % 1) * w;
        const sy = ((s.y - scrollY * 0.00012 * s.z) % 1 + 1) % 1 * h;
        ctx.fillStyle = "rgba(196,181,253," + (0.08 + 0.25 * s.z) * A + ")";
        ctx.fillRect(sx, sy, s.s, s.s);
      }

      // ring guide around hermes
      const [hx, hy] = P(N.hermes);
      ctx.beginPath(); ctx.arc(hx, hy, u * 1.45, 0, 6.283);
      ctx.strokeStyle = "rgba(168,85,247," + 0.08 * A + ")"; ctx.lineWidth = 1; ctx.stroke();
      ctx.beginPath(); ctx.arc(hx, hy, u * 0.62, 0, 6.283);
      ctx.strokeStyle = "rgba(168,85,247," + 0.1 * A + ")"; ctx.setLineDash([3, 7]); ctx.stroke(); ctx.setLineDash([]);

      // edges
      const curves = {};
      for (const [a, b] of EDGES) {
        const key = a + ">" + b;
        heat[key] = Math.max(0, (heat[key] || 0) - dt * 0.9);
        const inStage = STAGE_NODES[live].includes(a) && STAGE_NODES[live].includes(b);
        const base = stage >= 0 ? (inStage ? 0.28 : 0.05) : 0.14;
        curves[key] = drawEdge(a, b, (base + heat[key] * 0.5) * A, inStage ? AG[b].c === "#ffffff" ? "#c084fc" : AG[b].c : "#a855f7", 1 + heat[key] * 1.5);
      }

      // packets
      for (const pk of packets(tt)) {
        const key = pk.from + ">" + pk.to;
        heat[key] = Math.min(1, (heat[key] || 0) + dt * 2.5);
        const c = curves[key] || curves[pk.to + ">" + pk.from];
        if (!c) continue;
        const rev = !curves[key];
        const [x, y] = onCurve(c, rev ? 1 - pk.p : pk.p);
        // trail
        for (let i = 1; i <= 6; i++) {
          const q = clamp((rev ? 1 - pk.p : pk.p) - i * 0.03 * (rev ? -1 : 1), 0, 1);
          const [tx, ty] = onCurve(c, q);
          ctx.fillStyle = hexA(pk.c, (0.35 - i * 0.05) * A);
          ctx.beginPath(); ctx.arc(tx, ty, 2.6 - i * 0.3, 0, 6.283); ctx.fill();
        }
        glow(x, y, 16, pk.c, 0.55 * A);
        ctx.fillStyle = hexA(pk.c, A);
        ctx.beginPath(); ctx.arc(x, y, 3.2, 0, 6.283); ctx.fill();
        if (pk.p > 0.96) nodeHeat[pk.to] = 1;
      }

      // working pulses on specialists (2.9–4.3)
      const working = tt > 2.9 && tt < 4.3;
      const waiting = tt > 6.9 && tt < 8.6;

      // nodes
      for (const id in N) {
        const n = N[id], ag = AG[id], [x, y] = P(n);
        nodeHeat[id] = Math.max(0, (nodeHeat[id] || 0) - dt * 1.4);
        const inStage = STAGE_NODES[live].includes(id);
        const dim = stage >= 0 && !inStage ? 0.18 : 1;
        const r = n.r * u;
        const pulse = (SPEC.includes(id) && working) || (id === "approve" && waiting) ? 0.5 + 0.5 * Math.sin(T * 9) : 0;
        // halo
        glow(x, y, r * (2.6 + nodeHeat[id] * 1.2 + pulse * 0.6), ag.c, (0.16 + nodeHeat[id] * 0.35 + pulse * 0.18) * dim * A);
        // ring
        ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283);
        ctx.fillStyle = "rgba(10,5,22," + 0.85 * A + ")"; ctx.fill();
        ctx.lineWidth = id === "hermes" ? 1.6 : 1.2;
        ctx.strokeStyle = hexA(ag.c, (0.55 + nodeHeat[id] * 0.45 + pulse * 0.3) * dim * A); ctx.stroke();
        // inner
        if (id === "hermes") {
          const rot = T * 0.6;
          ctx.save(); ctx.translate(x, y); ctx.rotate(rot);
          ctx.strokeStyle = hexA(ag.c, 0.75 * dim * A); ctx.lineWidth = 1.4;
          ctx.strokeRect(-r * 0.42, -r * 0.42, r * 0.84, r * 0.84);
          ctx.rotate(0.785); ctx.strokeStyle = hexA(ag.c, 0.35 * dim * A);
          ctx.strokeRect(-r * 0.42, -r * 0.42, r * 0.84, r * 0.84);
          ctx.restore();
        } else if (id === "approve") {
          // human: head + shoulders
          ctx.fillStyle = hexA(ag.c, 0.85 * dim * A);
          ctx.beginPath(); ctx.arc(x, y - r * 0.22, r * 0.2, 0, 6.283); ctx.fill();
          ctx.beginPath(); ctx.arc(x, y + r * 0.42, r * 0.42, Math.PI, 0); ctx.fill();
          if (tt > 8.2 && tt < 9.8) { // tick after approval
            ctx.strokeStyle = hexA("#3ddc97", A); ctx.lineWidth = 2.2;
            ctx.beginPath(); ctx.moveTo(x + r * 0.45, y - r * 0.55); ctx.lineTo(x + r * 0.65, y - r * 0.35); ctx.lineTo(x + r * 1.0, y - r * 0.8); ctx.stroke();
          }
        } else if (id === "client") {
          ctx.strokeStyle = hexA(ag.c, 0.8 * dim * A); ctx.lineWidth = 1.4;
          ctx.strokeRect(x - r * 0.45, y - r * 0.3, r * 0.9, r * 0.6);
          ctx.beginPath(); ctx.moveTo(x - r * 0.45, y - r * 0.3); ctx.lineTo(x, y + r * 0.05); ctx.lineTo(x + r * 0.45, y - r * 0.3); ctx.stroke();
        } else {
          ctx.fillStyle = hexA(ag.c, (0.55 + pulse * 0.4) * dim * A);
          ctx.beginPath(); ctx.arc(x, y, r * 0.28, 0, 6.283); ctx.fill();
        }
        if (dim < 1) continue;
        // label
        ctx.textAlign = "center";
        ctx.font = "700 " + Math.max(9, Math.min(12, u * 0.085)) + "px Quicksand, system-ui, sans-serif";
        ctx.fillStyle = hexA(ag.c === "#ffffff" ? "#ede9fe" : ag.c, 0.85 * dim * A);
        ctx.fillText(ag.label, x, y + r + 15);
        if (ag.sub) {
          ctx.font = "600 " + Math.max(8, Math.min(10.5, u * 0.07)) + "px Quicksand, system-ui, sans-serif";
          ctx.fillStyle = "rgba(183,169,227," + 0.7 * dim * A + ")";
          ctx.fillText(ag.sub, x, y + r + 28);
        }
      }
      // waiting caption
      if (waiting) {
        const [x, y] = P(N.approve);
        ctx.font = "700 11px Quicksand, system-ui, sans-serif"; ctx.textAlign = "center";
        ctx.fillStyle = "rgba(255,255,255," + (0.5 + 0.4 * Math.sin(T * 6)) * A + ")";
        ctx.fillText("waiting for approval…", x, y - N.approve.r * u - 14);
      }
    }
    function loop(now) { frame(now); raf = requestAnimationFrame(loop); }
    if (still) frame(performance.now()); else raf = requestAnimationFrame(loop);
    document.addEventListener("visibilitychange", () => {
      if (still) return;
      if (document.hidden) cancelAnimationFrame(raf); else { last = performance.now(); raf = requestAnimationFrame(loop); }
    });
    return {
      setStage(n) { stage = n == null ? -1 : n; },
      setScroll(y) { scrollY = y; },
      stop() { cancelAnimationFrame(raf); },
    };
  }

  const instances = [];
  window.Orbit = {
    mount(c, o) { const i = mount(c, o); instances.push(i); return i; },
    setStage(n) { instances.forEach((i) => i.setStage(n)); },
    setScroll(y) { instances.forEach((i) => i.setScroll(y)); },
  };
  document.querySelectorAll("canvas[data-orbit]").forEach((c) => {
    let o = {};
    try { o = JSON.parse(c.dataset.orbit || "{}"); } catch (e) {}
    window.Orbit.mount(c, o);
  });
  if (!window.Cinema) window.addEventListener("scroll", () => window.Orbit.setScroll(window.scrollY), { passive: true });
})();
