/* Live desk graph: the work as it flows. You -> Atlas -> specialists -> the approval queue / the desk's records.
   Canvas 2D, light theme. Fed by live.js (LVG.on(...)); reads liveState for each agent's status. */
const LVG = (() => {
  const G = { cv: null, ctx: null, w: 0, h: 0, dpr: 1, view: { x: 0, y: 0, k: 1 }, drag: null, pos: {}, deskKey: '', nodes: {}, order: [], parts: [], chips: [], ripples: [], counts: { out: 0, desk: 0 }, bump: { out: 0, desk: 0 },
              raf: 0, last: 0, hover: null, active: false, youText: 'You', replay: null };
  const RGB = h => { const m = String(h).replace('#', ''); const v = m.length === 3 ? m.split('').map(c => c + c).join('') : m; const n = parseInt(v, 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
  const rgba = (h, a) => { const [r, g, b] = RGB(h); return `rgba(${r},${g},${b},${a})`; };
  const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const B = { grid: 'rgba(255,255,255,.07)', edge: 'rgba(255,255,255,.16)', card: '#FFFFFF', cardLine: 'rgba(255,255,255,.14)', chipBg: '#1B2537' };
  const T = () => ({ text: css('--text') || '#111827', text2: css('--text2') || '#374151', muted: css('--muted') || '#6B7280', faint: css('--faint') || '#9AA3B2', line: css('--line') || '#E3E8EF', line2: css('--line2') || '#D3DAE4', panel: css('--panel') || '#fff', panel2: css('--panel2') || '#F3F5F9', ok: css('--ok') || '#1A9E5C', warn: css('--warn') || '#B7791F', danger: css('--danger') || '#D64545', accent: css('--accent') || '#2F6FE4' });

  function mount(){
    G.cv = document.getElementById('lv-graph'); if (!G.cv) return;
    G.ctx = G.cv.getContext('2d');
    if (!G.cv._wired) { G.cv._wired = true; G.cv.addEventListener('pointerdown', onDown); G.cv.addEventListener('pointermove', onMove); G.cv.addEventListener('pointerup', onUp); G.cv.addEventListener('pointercancel', onUp); G.cv.addEventListener('mouseleave', () => { if (!G.drag) G.hover = null; }); G.cv.addEventListener('wheel', onWheel, { passive: false }); G.cv.addEventListener('dblclick', ev => { const m = mouse(ev), nd = hit(m.x, m.y); if (nd && (nd.kind === 'agent' || nd.kind === 'lead')) editAgent(nd.id); else fit(); }); new ResizeObserver(resize).observe(G.cv.parentElement); }
    G.deskKey = 'lv-pos-' + ((typeof CFG !== 'undefined' && CFG.desk && CFG.desk.id) || 'x'); try { G.pos = JSON.parse(localStorage.getItem(G.deskKey) || '{}'); } catch (_) { G.pos = {}; }
    resize();
  }
  function resize(){
    if (!G.cv) return; const r = G.cv.parentElement.getBoundingClientRect(); G.dpr = Math.min(2, window.devicePixelRatio || 1);
    G.w = Math.max(320, r.width); G.h = Math.max(240, r.height); G.cv.width = G.w * G.dpr; G.cv.height = G.h * G.dpr; G.cv.style.width = G.w + 'px'; G.cv.style.height = G.h + 'px';
    layout(); if (!G.userView) fit(); else draw(0);
  }
  /* ---- nodes */
  function build(agents){
    G.nodes = {}; G.order = []; G.parts = []; G.chips = []; G.ripples = []; G.counts = { out: 0, desk: 0 };
    // the Agents page arrangement (saved with the team) is the default layout here, scaled into the board
    G.teamPos = null; const withPos = agents.filter(a => a.pos && isFinite(a.pos.x)); if (withPos.length >= 2) { const xs = withPos.map(a => +a.pos.x), ys = withPos.map(a => +a.pos.y); const x0 = Math.min(...xs), x1 = Math.max(...xs) + 220, y0 = Math.min(...ys), y1 = Math.max(...ys) + 90; const k = Math.min((G.w * .62) / Math.max(1, x1 - x0), (G.h - 60) / Math.max(1, y1 - y0)); G.teamPos = {}; withPos.forEach(a => { G.teamPos[a.id] = { x: Math.max(150, G.w * .22) + (a.pos.x - x0) * k + 100, y: 40 + (a.pos.y - y0) * k + 36 }; }); }
    G.nodes.you = { id: 'you', kind: 'you', name: 'You', sub: 'the owner', color: T().ok, w: 136, h: 58 };
    const lead = agents.find(a => a.id === 'atlas');
    G.nodes.atlas = { id: 'atlas', kind: 'lead', name: lead ? lead.name : 'Atlas', sub: lead ? lead.role : 'lead', color: (lead && lead.color) || '#ea580c', w: 224, h: 80 };
    agents.filter(a => a.id !== 'atlas').forEach(a => { G.nodes[a.id] = { id: a.id, kind: 'agent', name: a.name, sub: a.role, color: a.color || '#7c3aed', w: 206, h: 70 }; G.order.push(a.id); });
    (G.cams || []).forEach((cm, i) => { G.nodes['cam:' + cm.name] = { id: 'cam:' + cm.name, kind: 'cam', name: cm.name, sub: cm.live ? 'live · watched' : (cm.source_kind || 'camera'), color: '#7FB5FF', w: 150, h: 48, cam: cm }; });
    G.nodes.out = { id: 'out', kind: 'sink', name: 'Approval queue', sub: 'waits for you', color: T().warn, w: 178, h: 60, icon: 'inbox' };
    G.nodes.desk = { id: 'desk', kind: 'sink', name: 'Desk records', sub: 'cases · records · CRM', color: T().accent, w: 178, h: 60, icon: 'db' };
    layout(); if (!G.userView) fit();
  }
  function setCams(list){ G.cams = (list || []).map(c => ({ name: c.name, live: !!(c.status && c.status.state === 'live'), source_kind: c.source_kind })); }
  function addAgent(a){ if (G.nodes[a.id]) return; G.nodes[a.id] = { id: a.id, kind: 'agent', name: a.name, sub: a.role, color: a.color, w: 206, h: 70 }; G.order.push(a.id); layout(); }
  function layout(){
    const w = G.w, h = G.h, n = G.order.length || 1, pad = 18;
    const put = (nd, x, y) => { if (!nd) return; nd.x = x; nd.y = y; };
    const cams = Object.values(G.nodes).filter(nd => nd.kind === 'cam'); const hasCams = cams.length > 0;
    // widths scale with the board so five columns always fit: you/cams | atlas | agents | bins
    const k = 1;                                                        // full-size cards; the view is fitted to the board instead
    const camW = Math.round(150 * k), youW = Math.round(136 * k), atlasW = Math.round(224 * k), agW = Math.round(206 * k), binW = Math.round(178 * k);
    if (G.nodes.you) G.nodes.you.w = youW; if (G.nodes.atlas) G.nodes.atlas.w = atlasW; G.order.forEach(id => { if (G.nodes[id]) G.nodes[id].w = agW; }); cams.forEach(c => c.w = camW); ['out', 'desk'].forEach(id => { if (G.nodes[id]) G.nodes[id].w = binW; });
    const leftW = hasCams ? Math.max(camW, youW) : youW;
    const x0 = pad + leftW / 2;                                        // you + cameras
    const x3 = w - pad - binW / 2;                                      // bins
    const gap = Math.max(28, (w - pad * 2 - leftW - atlasW - agW - binW) / 3);
    const x1 = pad + leftW + gap + atlasW / 2, x2 = x1 + atlasW / 2 + gap + agW / 2;
    const youY = hasCams ? Math.max(40, h / 2 - (cams.length * 56) / 2 - 20) : h / 2;
    put(G.nodes.you, x0, youY);
    cams.forEach((cm, i) => put(cm, x0, youY + 50 + i * 56 + 6));
    put(G.nodes.atlas, x1, h / 2);
    const top = pad + 62, bot = h - pad - 46, step = n > 1 ? (bot - top) / (n - 1) : 0;
    G.order.forEach((id, i) => put(G.nodes[id], x2, n > 1 ? top + i * step : h / 2));
    put(G.nodes.out, x3, h * 0.32); put(G.nodes.desk, x3, h * 0.70);
    Object.values(G.nodes).forEach(nd => { const p = G.pos[nd.id] || (G.teamPos && G.teamPos[nd.id]); if (p && !nd.moved) { nd.x = p.x; nd.y = p.y; nd.moved = true; } });
  }
  function savePos(){ try { localStorage.setItem(G.deskKey, JSON.stringify(G.pos)); } catch (_) {} }
  function resetLayout(){ G.pos = {}; savePos(); Object.values(G.nodes).forEach(nd => nd.moved = false); G.userView = false; layout(); fit(); }
  function fit(){ const ns = Object.values(G.nodes); if (!ns.length) return; const x0 = Math.min(...ns.map(n => n.x - n.w / 2)) - 24, x1 = Math.max(...ns.map(n => n.x + n.w / 2)) + 24, y0 = Math.min(...ns.map(n => n.y - n.h / 2)) - 24, y1 = Math.max(...ns.map(n => n.y + n.h / 2)) + 40; const top = 46, bottom = 30, k = Math.min(1.6, Math.max(.35, Math.min(G.w / (x1 - x0), (G.h - top - bottom) / (y1 - y0)))); G.view = { k, x: (G.w - (x1 - x0) * k) / 2 - x0 * k, y: top + ((G.h - top - bottom) - (y1 - y0) * k) / 2 - y0 * k }; redraw(); }
  function zoom(f, cx, cy){ cx = cx == null ? G.w / 2 : cx; cy = cy == null ? G.h / 2 : cy; const k2 = Math.min(2.2, Math.max(.35, G.view.k * f)); G.view.x = cx - (cx - G.view.x) * (k2 / G.view.k); G.view.y = cy - (cy - G.view.y) * (k2 / G.view.k); G.view.k = k2; redraw(); }
  const toWorld = (mx, my) => ({ x: (mx - G.view.x) / G.view.k, y: (my - G.view.y) / G.view.k });
  const port = (nd, side) => ({ x: nd.x + (side === 'r' ? nd.w / 2 : -nd.w / 2), y: nd.y });
  function curve(a, b){ const p = port(a, 'r'), q = port(b, 'l'); const mx = (p.x + q.x) / 2; return { p, q, c1: { x: mx, y: p.y }, c2: { x: mx, y: q.y } }; }
  const bez = (c, t) => { const u = 1 - t; return { x: u*u*u*c.p.x + 3*u*u*t*c.c1.x + 3*u*t*t*c.c2.x + t*t*t*c.q.x, y: u*u*u*c.p.y + 3*u*u*t*c.c1.y + 3*u*t*t*c.c2.y + t*t*t*c.q.y }; };
  const edgeOf = (from, to) => { const A = G.nodes[from], B = G.nodes[to]; if (!A || !B) return null; return A.x <= B.x ? { c: curve(A, B), rev: false } : { c: curve(B, A), rev: true }; };

  /* ---- effects */
  function flow(from, to, color, n, opts){ const e = edgeOf(from, to); if (!e) return; opts = opts || {};
    for (let i = 0; i < n; i++) G.parts.push({ e, t: -i * (opts.gap || 0.09), speed: opts.speed || 0.55, color, r: opts.r || 3.2, onEnd: i === n - 1 ? opts.onEnd : null, label: i === 0 ? opts.label : null }); wake(); }
  function ripple(id, color){ const nd = G.nodes[id]; if (!nd) return; G.ripples.push({ x: nd.x, y: nd.y, w: nd.w, h: nd.h, color, t: 0 }); wake(); }
  function chip(id, text, color){ const nd = G.nodes[id]; if (!nd) return; const mine = G.chips.filter(c => c.id === id); if (mine.length >= 3) { mine[0].life = Math.min(mine[0].life, mine[0].t + .25); } const k = Math.min(2, mine.length); G.chips.push({ id, x: nd.x + nd.w / 2 - 14, y: Math.max(14, nd.y - nd.h / 2 + 8 - k * 15), text, color, t: 0, life: 1.9 }); wake(); }
  function count(id){ G.counts[id]++; G.bump[id] = 1; wake(); }

  /* ---- events from live.js */
  function on(kind, a, x){
    if (!G.cv) return; const t = T();
    const id = a ? (G.nodes[a.id] ? a.id : a.base) : null;
    if (a && !G.nodes[a.id] && a.base !== 'atlas') { addAgent(a); }
    if (kind === 'start') { G.youText = 'You'; flow('you', 'atlas', t.ok, 6, { speed: .7, label: 'the job' }); ripple('atlas', G.nodes.atlas.color); }
    else if (kind === 'handoff') { flow('atlas', id, G.nodes[id] ? G.nodes[id].color : '#7c3aed', 9, { speed: .6, label: 'brief', onEnd: () => ripple(id, G.nodes[id].color) }); }
    else if (kind === 'tool' && /^camera_(look|events|ask)$/.test(x || '')) { const cams = Object.values(G.nodes).filter(n => n.kind === 'cam'); const name = x; chip(id || 'atlas', name, a ? a.color : '#ea580c'); const srcs = cams.length ? cams : []; srcs.slice(0, 5).forEach(cm => flow(cm.id, id || 'atlas', cm.color, 2, { speed: .9, r: 2.6 })); }
    else if (kind === 'tool') { const name = x || ''; if (name !== 'delegate' && name !== 'finish') chip(id || 'atlas', name, a ? a.color : '#ea580c'); if (/^(record_save|case_open|case_update|crm_update)$/.test(name)) flow(id || 'atlas', 'desk', t.accent, 2, { speed: .9, r: 2.6, onEnd: () => count('desk') }); }
    else if (kind === 'approval') { flow(id || 'atlas', 'out', t.warn, 3, { speed: .7, r: 3.4, label: x || 'draft', onEnd: () => { count('out'); ripple('out', t.warn); } }); }
    else if (kind === 'policy') { ripple(id || 'atlas', t.danger); chip(id || 'atlas', 'blocked by policy', t.danger); }
    else if (kind === 'back') { flow(id, 'atlas', G.nodes[id] ? G.nodes[id].color : '#7c3aed', 5, { speed: .7, label: 'report', onEnd: () => ripple('atlas', G.nodes.atlas.color) }); }
    else if (kind === 'done') { flow('atlas', 'you', t.ok, 7, { speed: .7, label: 'finished', onEnd: () => { ripple('you', t.ok); G.youText = 'You · done'; } }); }
    else if (kind === 'error') { ripple(id || 'atlas', t.danger); }
  }
  function setCounts(out, desk){ G.counts.out = out; G.counts.desk = desk; }

  /* ---- drawing */
  function wake(){ if (!G.raf) { G.last = performance.now(); G.raf = requestAnimationFrame(frame); } }
  function frame(now){ const dt = Math.min(.05, (now - G.last) / 1000); G.last = now; step(dt); draw(now / 1000); const busy = G.parts.length || G.ripples.length || G.chips.length || (liveState && liveState.active) || Object.values(G.bump).some(v => v > 0) || G.hover; G.raf = busy && page === 'live' ? requestAnimationFrame(frame) : 0; }
  function step(dt){
    G.parts.forEach(p => { p.t += p.speed * dt; }); G.parts = G.parts.filter(p => { if (p.t >= 1) { if (p.onEnd) p.onEnd(); return false; } return true; });
    G.ripples.forEach(r => r.t += dt * 1.6); G.ripples = G.ripples.filter(r => r.t < 1);
    G.chips.forEach(c => c.t += dt); G.chips = G.chips.filter(c => c.t < c.life);
    Object.keys(G.bump).forEach(k => { if (G.bump[k] > 0) G.bump[k] = Math.max(0, G.bump[k] - dt * 2.2); });
  }
  function rr(x, y, w, h, r){ const c = G.ctx; c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
  function state(id){ if (!liveState) return null; return liveState.agents[id] || null; }
  function draw(now){
    const c = G.ctx; if (!c) return; const t = T(); c.setTransform(G.dpr, 0, 0, G.dpr, 0, 0); c.clearRect(0, 0, G.w, G.h);
    // aurora: two slow blobs in the lead's and the busiest agent's colours
    const busyA = liveState ? Object.values(liveState.agents).filter(a => !a.done && a.status !== 'idle') : [];
    const c1 = G.nodes.atlas ? G.nodes.atlas.color : '#2F6FE4', c2 = busyA.length ? busyA[busyA.length - 1].color : '#5B93F0';
    const bx = G.w * (0.35 + 0.1 * Math.sin(now * .25)), by = G.h * (0.4 + 0.1 * Math.cos(now * .2)); let rg = c.createRadialGradient(bx, by, 0, bx, by, G.w * .45); rg.addColorStop(0, rgba(c1, .13)); rg.addColorStop(1, rgba(c1, 0)); c.fillStyle = rg; c.fillRect(0, 0, G.w, G.h);
    const ex = G.w * (0.7 + 0.08 * Math.cos(now * .18)), ey = G.h * (0.6 + 0.12 * Math.sin(now * .22)); rg = c.createRadialGradient(ex, ey, 0, ex, ey, G.w * .4); rg.addColorStop(0, rgba(c2, busyA.length ? .16 : .08)); rg.addColorStop(1, rgba(c2, 0)); c.fillStyle = rg; c.fillRect(0, 0, G.w, G.h);
    // dot grid, in screen space but moving with the view
    const gs = 24 * G.view.k, ox = ((G.view.x % gs) + gs) % gs, oy = ((G.view.y % gs) + gs) % gs; c.fillStyle = B.grid; for (let x = ox; x < G.w; x += gs) for (let y = oy; y < G.h; y += gs) { c.fillRect(x, y, 1.4, 1.4); }
    c.setTransform(G.dpr * G.view.k, 0, 0, G.dpr * G.view.k, G.dpr * G.view.x, G.dpr * G.view.y);
    // edges
    const drawEdge = (from, to, color, mode) => { const e = edgeOf(from, to); if (!e) return; const k = e.c; c.beginPath(); c.moveTo(k.p.x, k.p.y); c.bezierCurveTo(k.c1.x, k.c1.y, k.c2.x, k.c2.y, k.q.x, k.q.y);
      if (mode === 'on') { c.strokeStyle = rgba(color, .95); c.lineWidth = 2.2; c.setLineDash([7, 9]); c.lineDashOffset = -now * 40; c.shadowColor = rgba(color, .6); c.shadowBlur = 8; } else if (mode === 'done') { c.strokeStyle = rgba(color, .55); c.lineWidth = 1.6; c.setLineDash([]); } else { c.strokeStyle = B.edge; c.lineWidth = 1.2; c.setLineDash([]); } c.stroke(); c.setLineDash([]); c.shadowBlur = 0; };
    const S = liveState; const lead = state('atlas');
    drawEdge('you', 'atlas', t.ok, lead ? (S.active ? 'on' : 'done') : '');
    Object.values(G.nodes).filter(n => n.kind === 'cam').forEach(cm => drawEdge(cm.id, 'atlas', cm.color, cm.cam && cm.cam.live ? 'done' : ''));
    G.order.forEach(id => { const s = state(id); drawEdge('atlas', id, G.nodes[id].color, s ? (s.done ? 'done' : 'on') : ''); });
    drawEdge('atlas', 'out', t.warn, G.counts.out ? 'done' : ''); drawEdge('atlas', 'desk', t.accent, G.counts.desk ? 'done' : '');
    // particles
    G.parts.forEach(p => { if (p.t < 0) return; const tt = p.e.rev ? 1 - p.t : p.t; const pt = bez(p.e.c, tt); c.beginPath(); c.arc(pt.x, pt.y, p.r, 0, Math.PI * 2); c.fillStyle = p.color; c.shadowColor = rgba(p.color, .7); c.shadowBlur = 10; c.fill(); c.shadowBlur = 0;
      if (p.label && p.t > .08 && p.t < .92) { c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; c.fillStyle = '#E6ECF7'; c.fillText(p.label, pt.x + 8, pt.y - 7); } });
    // ripples
    G.ripples.forEach(r => { const k = r.t; c.strokeStyle = rgba(r.color, (1 - k) * .7); c.lineWidth = 2; rr(r.x - r.w / 2 - k * 16, r.y - r.h / 2 - k * 16, r.w + k * 32, r.h + k * 32, 14 + k * 8); c.stroke(); });
    // nodes
    Object.values(G.nodes).forEach(nd => drawNode(nd, now, t));
    // chips
    G.chips.forEach(ch => { const k = ch.t / ch.life, y = ch.y - k * 22, a = k < .15 ? k / .15 : (1 - (k - .15) / .85); c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; const w = c.measureText(ch.text).width + 12;
      rr(ch.x, y - 9, w, 17, 6); c.fillStyle = rgba(B.chipBg, .96 * a); c.fill(); c.strokeStyle = rgba(ch.color, .9 * a); c.lineWidth = 1; c.stroke(); c.fillStyle = rgba('#F3F6FB', a); c.fillText(ch.text, ch.x + 6, y + 3); });
    // hover tooltip
    if (G.hover) tooltip(G.hover, t);
  }
  function drawCam(nd, now, t){ const c = G.ctx, x = nd.x - nd.w / 2, y = nd.y - nd.h / 2, live = nd.cam && nd.cam.live;
    c.shadowColor = 'rgba(0,0,0,.35)'; c.shadowBlur = 12; c.shadowOffsetY = 3; rr(x, y, nd.w, nd.h, 10); c.fillStyle = '#1B2537'; c.fill(); c.shadowBlur = 0; c.shadowOffsetY = 0; c.strokeStyle = live ? rgba(nd.color, .8) : 'rgba(255,255,255,.12)'; c.lineWidth = 1; c.stroke();
    c.strokeStyle = nd.color; c.lineWidth = 1.4; rr(x + 12, y + 15, 16, 12, 2); c.stroke(); c.beginPath(); c.moveTo(x + 28, y + 19); c.lineTo(x + 34, y + 15); c.lineTo(x + 34, y + 27); c.lineTo(x + 28, y + 23); c.closePath(); c.stroke();
    c.fillStyle = '#E6ECF7'; c.font = '600 12px Inter, system-ui, sans-serif'; c.fillText(clip(nd.name, nd.w - 62), x + 42, y + 20);
    c.fillStyle = live ? '#4ADE80' : '#8FA0BA'; c.font = '500 10px "IBM Plex Mono", Consolas, monospace'; c.fillText(live ? 'live' : (nd.sub || ''), x + 42, y + 35);
    if (live) { const k = (now * 1.2) % 1; c.beginPath(); c.arc(x + nd.w - 14, y + 14, 3 + k * 5, 0, Math.PI * 2); c.strokeStyle = rgba('#4ADE80', (1 - k) * .8); c.lineWidth = 1.5; c.stroke(); c.beginPath(); c.arc(x + nd.w - 14, y + 14, 3, 0, Math.PI * 2); c.fillStyle = '#4ADE80'; c.fill(); } }
  function drawNode(nd, now, t){
    if (nd.kind === 'cam') return drawCam(nd, now, t);
    const c = G.ctx, s = nd.kind === 'agent' || nd.kind === 'lead' ? state(nd.id) : null;
    const idle = (nd.kind === 'agent') && !s, busy = s && !s.done && s.status !== 'idle', done = s && s.done;
    const x = nd.x - nd.w / 2, y = nd.y - nd.h / 2;
    c.globalAlpha = idle ? .78 : 1;
    if (busy) { c.shadowColor = rgba(nd.color, .55); c.shadowBlur = 28; } else { c.shadowColor = 'rgba(0,0,0,.35)'; c.shadowBlur = 16; c.shadowOffsetY = 4; }
    rr(x, y, nd.w, nd.h, 12); c.fillStyle = idle ? '#E9EEF6' : B.card; c.fill(); c.shadowBlur = 0; c.shadowOffsetY = 0;
    if (!idle) { const gr = c.createLinearGradient(x, y, x + nd.w, y + nd.h); gr.addColorStop(0, rgba(nd.color, 0)); gr.addColorStop(1, rgba(nd.color, busy ? .22 : .10)); c.fillStyle = gr; c.fill(); }
    c.strokeStyle = busy ? rgba(nd.color, 1) : done ? rgba(nd.color, .6) : 'rgba(16,24,40,.12)'; c.lineWidth = busy ? 1.8 : 1; c.stroke();
    if (G.drag && G.drag.nd === nd) { c.strokeStyle = rgba('#5B93F0', .9); c.lineWidth = 1.5; c.setLineDash([4, 4]); rr(x - 4, y - 4, nd.w + 8, nd.h + 8, 15); c.stroke(); c.setLineDash([]); }
    // colour bar
    c.fillStyle = rgba(nd.color, idle ? .5 : 1); rr(x + 1, y + 10, 3, nd.h - 20, 2); c.fill();
    // status ring
    const sx = x + nd.w - 16, sy = y + 15;
    if (busy) { c.beginPath(); c.arc(sx, sy, 6, now * 4, now * 4 + Math.PI * 1.3); c.strokeStyle = nd.color; c.lineWidth = 2; c.stroke(); c.beginPath(); c.arc(sx, sy, 2.2, 0, Math.PI * 2); c.fillStyle = nd.color; c.fill(); }
    else if (done) { c.beginPath(); c.arc(sx, sy, 6, 0, Math.PI * 2); c.fillStyle = rgba(t.ok, .15); c.fill(); c.strokeStyle = t.ok; c.lineWidth = 1.6; c.beginPath(); c.moveTo(sx - 3, sy); c.lineTo(sx - .8, sy + 2.4); c.lineTo(sx + 3.4, sy - 2.6); c.stroke(); }
    else if (nd.kind === 'sink') { const n = G.counts[nd.id], b = G.bump[nd.id] || 0; c.font = `600 ${15 + b * 6}px Inter, system-ui, sans-serif`; c.fillStyle = n ? nd.color : t.faint; c.textAlign = 'right'; c.fillText(String(n), x + nd.w - 12, y + 24); c.textAlign = 'left'; }
    else { c.beginPath(); c.arc(sx, sy, 3, 0, Math.PI * 2); c.fillStyle = t.line2; c.fill(); }
    // avatar: initials in the agent's colour (a person on an org chart), an icon for the bins
    const ax = x + 14, ay = y + (nd.kind === 'sink' ? 15 : 14), ar = nd.kind === 'lead' ? 15 : 13;
    if (nd.kind !== 'sink') { c.beginPath(); c.arc(ax + ar, ay + ar, ar, 0, Math.PI * 2); c.fillStyle = rgba(nd.color, idle ? .25 : .16); c.fill(); c.strokeStyle = rgba(nd.color, idle ? .5 : .9); c.lineWidth = 1.2; c.stroke();
      const ini = nd.id === 'you' ? 'Y' : String(nd.name).split(/[\s&]+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase(); c.fillStyle = nd.color; c.font = `700 ${nd.kind === 'lead' ? 12 : 11}px Inter, system-ui, sans-serif`; c.textAlign = 'center'; c.fillText(ini, ax + ar, ay + ar + 4); c.textAlign = 'left'; }
    else { c.strokeStyle = nd.color; c.lineWidth = 1.5; if (nd.icon === 'inbox') { rr(ax + 2, ay + 4, 20, 16, 3); c.stroke(); c.beginPath(); c.moveTo(ax + 2, ay + 13); c.lineTo(ax + 8, ay + 13); c.lineTo(ax + 10, ay + 16); c.lineTo(ax + 14, ay + 16); c.lineTo(ax + 16, ay + 13); c.lineTo(ax + 22, ay + 13); c.stroke(); }
      else { c.beginPath(); c.ellipse(ax + 12, ay + 6, 9, 3.5, 0, 0, Math.PI * 2); c.stroke(); c.beginPath(); c.moveTo(ax + 3, ay + 6); c.lineTo(ax + 3, ay + 18); c.ellipse(ax + 12, ay + 18, 9, 3.5, 0, Math.PI, 0, true); c.lineTo(ax + 21, ay + 6); c.stroke(); } }
    const tx = x + 14 + (nd.kind === 'sink' ? 30 : ar * 2 + 10);
    // text
    c.fillStyle = t.text; c.font = `600 ${nd.kind === 'lead' ? 14 : 13}px Inter, system-ui, sans-serif`; c.fillText(clip(nd.id === 'you' ? G.youText : nd.name, nd.w - (tx - x) - 28), tx, y + 20);
    let sub = nd.sub; if (s && s.assignment && nd.kind === 'agent') sub = s.assignment; if (nd.kind === 'lead' && liveState) sub = liveState.task.split('\n')[0];
    c.fillStyle = s && s.assignment && nd.kind === 'agent' ? t.text2 : t.muted; c.font = `${s && s.assignment ? 'italic ' : ''}400 11px Inter, system-ui, sans-serif`; c.fillText(clip(sub, nd.w - (tx - x) - 12), tx, y + 36);
    if (s && nd.kind !== 'sink' && typeof LVC !== 'undefined') { LVC.spark(c, s, liveState, x + nd.w - 72, y + nd.h - 24, 58, 12); }
    if (s && nd.kind !== 'sink') { const line = busy ? (s.status === 'tool' ? s.now : s.status === 'writing' ? 'writing…' : 'thinking…') : done ? `${s.turns} turn${s.turns === 1 ? '' : 's'} · ${s.tools} tool call${s.tools === 1 ? '' : 's'}` : '';
      c.fillStyle = busy ? nd.color : t.faint; c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; c.fillText(clip(line, nd.w - 100), x + 14, y + nd.h - 11); }
    else if (nd.kind === 'sink') { c.fillStyle = t.faint; c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; c.fillText(nd.id === 'out' ? (G.counts.out ? `${G.counts.out} held for approval` : 'nothing queued yet') : (G.counts.desk ? `${G.counts.desk} writes` : 'nothing written yet'), x + 14, y + nd.h - 11); }
    if (G.hover === nd && (nd.kind === 'agent' || nd.kind === 'lead')) { c.fillStyle = rgba(nd.color, .12); rr(x + nd.w - 72, y + nd.h - 22, 62, 16, 6); c.fill(); c.fillStyle = nd.color; c.font = '600 9.5px Inter, system-ui, sans-serif'; c.fillText('configure ›', x + nd.w - 66, y + nd.h - 11); }
    if (typeof LVI !== 'undefined' && LVI.state.open && LVI.state.open === nd.id) { c.strokeStyle = rgba(nd.color, .9); c.lineWidth = 2; c.setLineDash([5, 4]); c.lineDashOffset = -now * 20; rr(x - 5, y - 5, nd.w + 10, nd.h + 10, 15); c.stroke(); c.setLineDash([]); }
    c.globalAlpha = 1;
  }
  function clip(s, w){ const c = G.ctx; s = String(s || ''); if (c.measureText(s).width <= w) return s; while (s.length > 1 && c.measureText(s + '…').width > w) s = s.slice(0, -1); return s + '…'; }
  function tooltip(nd, t){ const c = G.ctx, s = state(nd.id); const lines = [nd.name]; if (s && s.assignment) lines.push(...wrap(s.assignment, 54)); else if (nd.sub) lines.push(nd.sub); if (s) lines.push(`${s.status} · ${s.turns} turns · ${s.tools} tool calls`);
    c.font = '400 11.5px Inter, system-ui, sans-serif'; const w = Math.max(...lines.map(l => c.measureText(l).width)) + 20, h = lines.length * 16 + 14; let x = nd.x - w / 2, y = nd.y - nd.h / 2 - h - 8; if (y < 4) y = nd.y + nd.h / 2 + 8; x = Math.max(6, Math.min(G.w - w - 6, x));
    c.shadowColor = 'rgba(0,0,0,.4)'; c.shadowBlur = 18; rr(x, y, w, h, 9); c.fillStyle = '#1B2537'; c.fill(); c.shadowBlur = 0; c.strokeStyle = '#2E3F5C'; c.lineWidth = 1; c.stroke();
    lines.forEach((l, i) => { c.fillStyle = i === 0 ? '#F3F6FB' : '#C7D2E3'; c.font = i === 0 ? '600 12px Inter, system-ui, sans-serif' : '400 11.5px Inter, system-ui, sans-serif'; c.fillText(l, x + 10, y + 18 + i * 16); }); }
  function wrap(s, n){ const out = []; let line = ''; String(s).split(/\s+/).forEach(w => { if ((line + ' ' + w).trim().length > n) { out.push(line.trim()); line = w; } else line += ' ' + w; }); if (line.trim()) out.push(line.trim()); return out.slice(0, 4); }
  function hit(mx, my){ const p = toWorld(mx, my); return Object.values(G.nodes).find(nd => Math.abs(p.x - nd.x) <= nd.w / 2 && Math.abs(p.y - nd.y) <= nd.h / 2) || null; }
  const mouse = ev => { const r = G.cv.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
  function onDown(ev){ if (ev.button !== 0) return; const m = mouse(ev), nd = hit(m.x, m.y); G.cv.setPointerCapture(ev.pointerId);
    G.drag = nd ? { nd, dx: toWorld(m.x, m.y).x - nd.x, dy: toWorld(m.x, m.y).y - nd.y, moved: false, sx: m.x, sy: m.y } : { pan: true, sx: m.x, sy: m.y, vx: G.view.x, vy: G.view.y, moved: false }; }
  function onMove(ev){ const m = mouse(ev);
    if (G.drag) { const d = G.drag; if (Math.hypot(m.x - d.sx, m.y - d.sy) > 3) d.moved = true;
      if (d.pan) { G.userView = true; G.view.x = d.vx + (m.x - d.sx); G.view.y = d.vy + (m.y - d.sy); }
      else { const p = toWorld(m.x, m.y); d.nd.x = p.x - d.dx; d.nd.y = p.y - d.dy; d.nd.moved = true; }
      redraw(); return; }
    G.lastMouse = toWorld(m.x, m.y); const nd = hit(m.x, m.y); if (nd !== G.hover) { G.hover = nd; G.cv.style.cursor = nd ? 'grab' : 'default'; } draw(performance.now() / 1000); }
  function onUp(ev){ const d = G.drag; if (!d) return; G.drag = null; try { G.cv.releasePointerCapture(ev.pointerId); } catch (_) {}
    if (d.nd && d.moved) { G.pos[d.nd.id] = { x: Math.round(d.nd.x), y: Math.round(d.nd.y) }; savePos(); redraw(); return; }
    if (!d.moved && d.nd) onClick(d.nd); else redraw(); }
  function onClick(nd){ if (nd.id === 'out') go('approvals'); else if (nd.id === 'desk') location.href = '/desk/ops'; else if (nd.kind === 'cam') go('cameras');
    else if (nd.kind === 'agent' || nd.kind === 'lead') editAgent(nd.id); else if (typeof lvFocusAgent === 'function') lvFocusAgent(nd.id); }
  function editAgent(id){ if (typeof LVI !== 'undefined') { LVI.open(id.split('#')[0]); redraw(); } else go('team'); }
  function addAgentUI(){ if (typeof LVI !== 'undefined') { LVI.open(''); redraw(); } else go('team'); }
  function onWheel(ev){ ev.preventDefault(); G.userView = true; const m = mouse(ev); zoom(ev.deltaY < 0 ? 1.12 : 1 / 1.12, m.x, m.y); }
  function redraw(){ if (G.cv) { draw(performance.now() / 1000); wake(); } }
  return { mount, build, on, setCounts, redraw, resize, fit, zoom, resetLayout, setCams, editAgent, addAgentUI, state: G };
})();
