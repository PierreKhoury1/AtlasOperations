/* Hero: replay of a REAL orchestration run (window.ORCH from orch-data.js, captured 1 Sep 2026 on OpenRouter,
   model minimax/minimax-m3:free). Atlas streams a plan, spawns six specialists in parallel, each streams its answer,
   Atlas merges. Every token is drawn at the wall-clock offset it actually arrived at. Loops. */
(function () {
  const D = window.ORCH, root = document.getElementById("orch");
  if (!D || !root) return;
  const $ = (id) => document.getElementById(id);
  const canvas = $("orchCanvas"), ctx = canvas.getContext("2d"), panels = $("orchPanels"), merge = $("orchMerge");
  const rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const NAMES = Object.keys(D.plan.instructions);            // Vision, Retrieval, Reasoning, Booking, Writer, Policy
  const COL = { Vision: "#2d72d2", Retrieval: "#7c5cd6", Reasoning: "#1c8f5f", Booking: "#a66a00", Writer: "#c2412e", Policy: "#0e7c86" };
  const T_START = 4.6, T_END = D.merge.last + 1.2, HOLD = 3.2, ATLAS_DONE = D.plan.tokens[D.plan.tokens.length - 1][0];

  $("orchTask").textContent = D.task;
  $("orchDet").textContent = "detector: " + D.detections;
  const P = {};
  NAMES.forEach((n) => {
    const el = document.createElement("div"); el.className = "op"; el.style.setProperty("--c", COL[n] || "#2d72d2");
    el.innerHTML = "<div class='h'><b>" + n + "</b><span class='st'>waiting</span></div><div class='ins'>" + D.plan.instructions[n] + "</div><div class='b'></div>";
    panels.appendChild(el); P[n] = { el, b: el.querySelector(".b"), st: el.querySelector(".st") };
  });
  const mergeB = merge.querySelector("p"), mergeQ = merge.querySelector(".q");

  function upto(tokens, t) { let s = ""; for (const [ts, tok] of tokens) { if (ts > t) break; s += tok; } return s; }
  function count(tokens, t) { let n = 0; for (const [ts] of tokens) { if (ts > t) break; n++; } return n; }

  // ---- graph geometry -------------------------------------------------------------------------
  function layout() {
    const W = canvas.clientWidth, H = canvas.height; if (canvas.width !== W) canvas.width = W;
    const atlas = { x: 54, y: H / 2 }, mergeN = { x: W - 54, y: H / 2 };
    const gap = (H - 28) / (NAMES.length - 1);
    const agents = NAMES.map((n, i) => ({ n, x: W / 2, y: 14 + i * gap }));
    return { W, H, atlas, mergeN, agents };
  }
  function edge(a, b, col, prog, pulse, dash) {
    const mx = (a.x + b.x) / 2;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.bezierCurveTo(mx, a.y, mx, b.y, b.x, b.y);
    ctx.strokeStyle = col; ctx.lineWidth = 1.2; ctx.setLineDash(dash ? [3, 4] : []);
    if (prog < 1) { ctx.globalAlpha = 0.25; ctx.stroke(); ctx.globalAlpha = 1; ctx.setLineDash([]); return; }
    ctx.stroke(); ctx.setLineDash([]);
    if (pulse != null) {                                       // a dot travelling along the curve
      const t = pulse, u = 1 - t;
      const x = u * u * u * a.x + 3 * u * u * t * mx + 3 * u * t * t * mx + t * t * t * b.x;
      const y = u * u * u * a.y + 3 * u * u * t * a.y + 3 * u * t * t * b.y + t * t * t * b.y;
      ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill();
    }
  }
  function node(p, label, col, active, done, r) {
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = active ? col : "#ffffff"; ctx.strokeStyle = col; ctx.lineWidth = active ? 0 : 1.5; ctx.fill(); if (!active) ctx.stroke();
    if (active) { ctx.beginPath(); ctx.arc(p.x, p.y, r + 4 + 2 * Math.sin(performance.now() / 180), 0, Math.PI * 2); ctx.strokeStyle = col; ctx.globalAlpha = 0.35; ctx.lineWidth = 1; ctx.stroke(); ctx.globalAlpha = 1; }
    if (done) { ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.6; ctx.beginPath(); ctx.moveTo(p.x - 3.5, p.y); ctx.lineTo(p.x - 1, p.y + 2.6); ctx.lineTo(p.x + 3.8, p.y - 2.8); ctx.stroke(); }
    ctx.fillStyle = "#10161d"; ctx.font = "600 10.5px 'IBM Plex Mono', Consolas, monospace"; ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText(label, p.x, p.y + r + 5);
  }

  // ---- one frame at replay time t (seconds since the run's first request) ---------------------------
  function render(t) {
    const L = layout(); ctx.clearRect(0, 0, L.W, L.H);
    const spawned = t >= D.spawn_t, planning = t >= D.plan.tokens[0][0] && t < ATLAS_DONE;
    let tokens = count(D.plan.tokens, t), parallel = 0, doneN = 0, allDone = true;
    L.agents.forEach((a) => {
      const A = D.agents[a.n], col = COL[a.n], started = t >= A.first, done = t >= A.last + 0.15;
      if (started && !done) parallel++; if (done) doneN++; if (!done) allDone = false;
      tokens += count(A.tokens, t);
      edge(L.atlas, a, col, spawned ? 1 : 0, spawned && !started ? ((t - D.spawn_t) / (A.first - D.spawn_t)) % 1 : (started && !done ? (t * 1.3) % 1 : null), !spawned);
      edge(a, L.mergeN, col, done ? 1 : 0, done && t < D.merge.first ? ((t - A.last) / (D.merge.first - A.last)) % 1 : null, !done);
      node(a, a.n, col, started && !done, done, 7);
      const p = P[a.n];
      if (!started) { p.b.textContent = ""; p.st.textContent = spawned ? "assigned" : "waiting"; p.el.className = "op" + (spawned ? " asg" : ""); }
      else { p.b.textContent = upto(A.tokens, t); p.st.textContent = done ? "done" : "streaming"; p.el.className = "op " + (done ? "done" : "live"); }
    });
    const merging = t >= D.merge.first;
    tokens += count(D.merge.tokens, t);
    node(L.atlas, "ATLAS", "#10161d", planning || (allDone && merging && t < D.merge.last), false, 11);
    node(L.mergeN, "MERGE", "#1c8f5f", merging && t < D.merge.last, t >= D.merge.last, 9);
    if (merging) { mergeB.textContent = upto(D.merge.tokens, t); merge.classList.add("live"); mergeQ.hidden = t < D.merge.last + 0.3; }
    else { mergeB.textContent = allDone ? "" : ""; merge.classList.remove("live"); mergeQ.hidden = true; }
    $("stInst").textContent = 1 + (spawned ? L.agents.filter((a) => t >= D.agents[a.n].first).length : 0) + (merging ? 1 : 0);
    $("stTok").textContent = tokens;
    $("stPar").textContent = parallel;
    $("stWall").textContent = Math.max(0, Math.min(t, D.wall_s)).toFixed(1) + " s";
    $("orchTag").textContent = t < D.spawn_t ? "atlas · reading the job & planning" : (allDone ? (t >= D.merge.last ? "queued for approval · " + D.wall_s + " s end to end" : "atlas · merging") : parallel + " specialists streaming in parallel");
  }

  if (rm) { render(T_END); return; }
  let t0 = performance.now(), visible = true;
  function tick() {
    if (!visible) return;
    let t = T_START + (performance.now() - t0) / 1000;
    if (t > T_END + HOLD) { t0 = performance.now(); t = T_START; }
    render(Math.min(t, T_END));
    requestAnimationFrame(tick);
  }
  new IntersectionObserver((es) => { const on = es.some((e) => e.isIntersecting); if (on && !visible) { visible = true; t0 = performance.now(); tick(); } else if (!on) visible = false; }, { threshold: 0.05 }).observe(root);
  document.addEventListener("visibilitychange", () => { if (document.hidden) visible = false; else if (!visible) { visible = true; t0 = performance.now(); tick(); } });
  tick();
})();
