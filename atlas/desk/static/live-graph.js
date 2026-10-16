/* Live desk graph: the work as it flows. You -> Atlas -> specialists -> the approval queue / the desk's records.
   Canvas 2D, light theme. Fed by live.js (LVG.on(...)); reads liveState for each agent's status. */
const LVG = (() => {
  const G = { cv: null, ctx: null, w: 0, h: 0, dpr: 1, nodes: {}, order: [], parts: [], chips: [], ripples: [], counts: { out: 0, desk: 0 }, bump: { out: 0, desk: 0 },
              raf: 0, last: 0, hover: null, active: false, youText: 'You', replay: null };
  const RGB = h => { const m = String(h).replace('#', ''); const v = m.length === 3 ? m.split('').map(c => c + c).join('') : m; const n = parseInt(v, 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
  const rgba = (h, a) => { const [r, g, b] = RGB(h); return `rgba(${r},${g},${b},${a})`; };
  const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const T = () => ({ text: css('--text') || '#111827', text2: css('--text2') || '#374151', muted: css('--muted') || '#6B7280', faint: css('--faint') || '#9AA3B2', line: css('--line') || '#E3E8EF', line2: css('--line2') || '#D3DAE4', panel: css('--panel') || '#fff', panel2: css('--panel2') || '#F3F5F9', ok: css('--ok') || '#1A9E5C', warn: css('--warn') || '#B7791F', danger: css('--danger') || '#D64545', accent: css('--accent') || '#2F6FE4' });

  function mount(){
    G.cv = document.getElementById('lv-graph'); if (!G.cv) return;
    G.ctx = G.cv.getContext('2d');
    if (!G.cv._wired) { G.cv._wired = true; G.cv.addEventListener('mousemove', onMove); G.cv.addEventListener('mouseleave', () => { G.hover = null; }); G.cv.addEventListener('click', onClick); new ResizeObserver(resize).observe(G.cv.parentElement); }
    resize();
  }
  function resize(){
    if (!G.cv) return; const r = G.cv.parentElement.getBoundingClientRect(); G.dpr = Math.min(2, window.devicePixelRatio || 1);
    G.w = Math.max(320, r.width); G.h = Math.max(240, r.height); G.cv.width = G.w * G.dpr; G.cv.height = G.h * G.dpr; G.cv.style.width = G.w + 'px'; G.cv.style.height = G.h + 'px';
    layout(); draw(0);
  }
  /* ---- nodes */
  function build(agents){
    G.nodes = {}; G.order = []; G.parts = []; G.chips = []; G.ripples = []; G.counts = { out: 0, desk: 0 };
    G.nodes.you = { id: 'you', kind: 'you', name: 'You', sub: 'the owner', color: T().ok, w: 118, h: 54 };
    const lead = agents.find(a => a.id === 'atlas');
    G.nodes.atlas = { id: 'atlas', kind: 'lead', name: lead ? lead.name : 'Atlas', sub: lead ? lead.role : 'lead', color: (lead && lead.color) || '#ea580c', w: 196, h: 76 };
    agents.filter(a => a.id !== 'atlas').forEach(a => { G.nodes[a.id] = { id: a.id, kind: 'agent', name: a.name, sub: a.role, color: a.color || '#7c3aed', w: 184, h: 66 }; G.order.push(a.id); });
    G.nodes.out = { id: 'out', kind: 'sink', name: 'Approval queue', sub: 'waits for you', color: T().warn, w: 150, h: 60, icon: 'inbox' };
    G.nodes.desk = { id: 'desk', kind: 'sink', name: 'Desk records', sub: 'cases · records · CRM', color: T().accent, w: 150, h: 60, icon: 'db' };
    layout();
  }
  function addAgent(a){ if (G.nodes[a.id]) return; G.nodes[a.id] = { id: a.id, kind: 'agent', name: a.name, sub: a.role, color: a.color, w: 184, h: 66 }; G.order.push(a.id); layout(); }
  function layout(){
    const w = G.w, h = G.h, n = G.order.length || 1, pad = 18;
    const put = (nd, x, y) => { if (!nd) return; nd.x = x; nd.y = y; };
    put(G.nodes.you, pad + 59, h / 2);
    put(G.nodes.atlas, Math.max(230, w * 0.30), h / 2);
    const colX = Math.max(430, w * 0.60), top = pad + 40, bot = h - pad - 40, step = n > 1 ? (bot - top) / (n - 1) : 0;
    G.order.forEach((id, i) => put(G.nodes[id], colX, n > 1 ? top + i * step : h / 2));
    put(G.nodes.out, w - pad - 75, h * 0.30); put(G.nodes.desk, w - pad - 75, h * 0.70);
  }
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
  function frame(now){ const dt = Math.min(.05, (now - G.last) / 1000); G.last = now; step(dt); draw(now / 1000); const busy = G.parts.length || G.ripples.length || G.chips.length || (liveState && liveState.active) || Object.values(G.bump).some(v => v > 0); G.raf = busy && page === 'live' ? requestAnimationFrame(frame) : 0; }
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
    // dot grid
    c.fillStyle = rgba(t.line2, .55); for (let x = 14; x < G.w; x += 22) for (let y = 14; y < G.h; y += 22) { c.fillRect(x, y, 1.2, 1.2); }
    // edges
    const drawEdge = (from, to, color, mode) => { const e = edgeOf(from, to); if (!e) return; const k = e.c; c.beginPath(); c.moveTo(k.p.x, k.p.y); c.bezierCurveTo(k.c1.x, k.c1.y, k.c2.x, k.c2.y, k.q.x, k.q.y);
      if (mode === 'on') { c.strokeStyle = rgba(color, .9); c.lineWidth = 2; c.setLineDash([7, 9]); c.lineDashOffset = -now * 40; } else if (mode === 'done') { c.strokeStyle = rgba(color, .45); c.lineWidth = 1.6; c.setLineDash([]); } else { c.strokeStyle = t.line2; c.lineWidth = 1.2; c.setLineDash([]); } c.stroke(); c.setLineDash([]); };
    const S = liveState; const lead = state('atlas');
    drawEdge('you', 'atlas', t.ok, lead ? (S.active ? 'on' : 'done') : '');
    G.order.forEach(id => { const s = state(id); drawEdge('atlas', id, G.nodes[id].color, s ? (s.done ? 'done' : 'on') : ''); });
    drawEdge('atlas', 'out', t.warn, G.counts.out ? 'done' : ''); drawEdge('atlas', 'desk', t.accent, G.counts.desk ? 'done' : '');
    // particles
    G.parts.forEach(p => { if (p.t < 0) return; const tt = p.e.rev ? 1 - p.t : p.t; const pt = bez(p.e.c, tt); c.beginPath(); c.arc(pt.x, pt.y, p.r, 0, Math.PI * 2); c.fillStyle = p.color; c.shadowColor = rgba(p.color, .7); c.shadowBlur = 10; c.fill(); c.shadowBlur = 0;
      if (p.label && p.t > .08 && p.t < .92) { c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; c.fillStyle = rgba(p.color, .95); c.fillText(p.label, pt.x + 8, pt.y - 7); } });
    // ripples
    G.ripples.forEach(r => { const k = r.t; c.strokeStyle = rgba(r.color, (1 - k) * .7); c.lineWidth = 2; rr(r.x - r.w / 2 - k * 16, r.y - r.h / 2 - k * 16, r.w + k * 32, r.h + k * 32, 14 + k * 8); c.stroke(); });
    // nodes
    Object.values(G.nodes).forEach(nd => drawNode(nd, now, t));
    // chips
    G.chips.forEach(ch => { const k = ch.t / ch.life, y = ch.y - k * 22, a = k < .15 ? k / .15 : (1 - (k - .15) / .85); c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; const w = c.measureText(ch.text).width + 12;
      rr(ch.x, y - 9, w, 17, 6); c.fillStyle = rgba('#ffffff', .96 * a); c.fill(); c.strokeStyle = rgba(ch.color, .8 * a); c.lineWidth = 1; c.stroke(); c.fillStyle = rgba(ch.color, a); c.fillText(ch.text, ch.x + 6, y + 3); });
    // hover tooltip
    if (G.hover) tooltip(G.hover, t);
  }
  function drawNode(nd, now, t){
    const c = G.ctx, s = nd.kind === 'agent' || nd.kind === 'lead' ? state(nd.id) : null;
    const idle = (nd.kind === 'agent') && !s, busy = s && !s.done && s.status !== 'idle', done = s && s.done;
    const x = nd.x - nd.w / 2, y = nd.y - nd.h / 2;
    c.globalAlpha = idle ? .62 : 1;
    if (busy) { c.shadowColor = rgba(nd.color, .35); c.shadowBlur = 22; } else { c.shadowColor = 'rgba(16,24,40,.08)'; c.shadowBlur = 10; c.shadowOffsetY = 2; }
    rr(x, y, nd.w, nd.h, 12); c.fillStyle = t.panel; c.fill(); c.shadowBlur = 0; c.shadowOffsetY = 0;
    c.strokeStyle = busy ? rgba(nd.color, .9) : done ? rgba(nd.color, .5) : t.line2; c.lineWidth = busy ? 1.6 : 1; c.stroke();
    // colour bar
    c.fillStyle = rgba(nd.color, idle ? .5 : 1); rr(x + 1, y + 10, 3, nd.h - 20, 2); c.fill();
    // status ring
    const sx = x + nd.w - 16, sy = y + 15;
    if (busy) { c.beginPath(); c.arc(sx, sy, 6, now * 4, now * 4 + Math.PI * 1.3); c.strokeStyle = nd.color; c.lineWidth = 2; c.stroke(); c.beginPath(); c.arc(sx, sy, 2.2, 0, Math.PI * 2); c.fillStyle = nd.color; c.fill(); }
    else if (done) { c.beginPath(); c.arc(sx, sy, 6, 0, Math.PI * 2); c.fillStyle = rgba(t.ok, .15); c.fill(); c.strokeStyle = t.ok; c.lineWidth = 1.6; c.beginPath(); c.moveTo(sx - 3, sy); c.lineTo(sx - .8, sy + 2.4); c.lineTo(sx + 3.4, sy - 2.6); c.stroke(); }
    else if (nd.kind === 'sink') { const n = G.counts[nd.id], b = G.bump[nd.id] || 0; c.font = `600 ${15 + b * 6}px Inter, system-ui, sans-serif`; c.fillStyle = n ? nd.color : t.faint; c.textAlign = 'right'; c.fillText(String(n), x + nd.w - 12, y + 24); c.textAlign = 'left'; }
    else { c.beginPath(); c.arc(sx, sy, 3, 0, Math.PI * 2); c.fillStyle = t.line2; c.fill(); }
    // text
    c.fillStyle = t.text; c.font = `600 ${nd.kind === 'lead' ? 14 : 13}px Inter, system-ui, sans-serif`; c.fillText(clip(nd.id === 'you' ? G.youText : nd.name, nd.w - 44), x + 14, y + 20);
    let sub = nd.sub; if (s && s.assignment && nd.kind === 'agent') sub = s.assignment; if (nd.kind === 'lead' && liveState) sub = liveState.task.split('\n')[0];
    c.fillStyle = s && s.assignment && nd.kind === 'agent' ? t.text2 : t.muted; c.font = `${s && s.assignment ? 'italic ' : ''}400 11px Inter, system-ui, sans-serif`; c.fillText(clip(sub, nd.w - 26), x + 14, y + 36);
    if (s && nd.kind !== 'sink') { const line = busy ? (s.status === 'tool' ? s.now : s.status === 'writing' ? 'writing…' : 'thinking…') : done ? `${s.turns} turn${s.turns === 1 ? '' : 's'} · ${s.tools} tool call${s.tools === 1 ? '' : 's'}` : '';
      c.fillStyle = busy ? nd.color : t.faint; c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; c.fillText(clip(line, nd.w - 26), x + 14, y + nd.h - 11); }
    else if (nd.kind === 'sink') { c.fillStyle = t.faint; c.font = '500 10.5px "IBM Plex Mono", Consolas, monospace'; c.fillText(nd.id === 'out' ? (G.counts.out ? `${G.counts.out} held for approval` : 'nothing queued yet') : (G.counts.desk ? `${G.counts.desk} writes` : 'nothing written yet'), x + 14, y + nd.h - 11); }
    c.globalAlpha = 1;
  }
  function clip(s, w){ const c = G.ctx; s = String(s || ''); if (c.measureText(s).width <= w) return s; while (s.length > 1 && c.measureText(s + '…').width > w) s = s.slice(0, -1); return s + '…'; }
  function tooltip(nd, t){ const c = G.ctx, s = state(nd.id); const lines = [nd.name]; if (s && s.assignment) lines.push(...wrap(s.assignment, 54)); else if (nd.sub) lines.push(nd.sub); if (s) lines.push(`${s.status} · ${s.turns} turns · ${s.tools} tool calls`);
    c.font = '400 11.5px Inter, system-ui, sans-serif'; const w = Math.max(...lines.map(l => c.measureText(l).width)) + 20, h = lines.length * 16 + 14; let x = nd.x - w / 2, y = nd.y - nd.h / 2 - h - 8; if (y < 4) y = nd.y + nd.h / 2 + 8; x = Math.max(6, Math.min(G.w - w - 6, x));
    c.shadowColor = 'rgba(16,24,40,.18)'; c.shadowBlur = 18; rr(x, y, w, h, 9); c.fillStyle = t.panel; c.fill(); c.shadowBlur = 0; c.strokeStyle = t.line2; c.lineWidth = 1; c.stroke();
    lines.forEach((l, i) => { c.fillStyle = i === 0 ? t.text : t.text2; c.font = i === 0 ? '600 12px Inter, system-ui, sans-serif' : '400 11.5px Inter, system-ui, sans-serif'; c.fillText(l, x + 10, y + 18 + i * 16); }); }
  function wrap(s, n){ const out = []; let line = ''; String(s).split(/\s+/).forEach(w => { if ((line + ' ' + w).trim().length > n) { out.push(line.trim()); line = w; } else line += ' ' + w; }); if (line.trim()) out.push(line.trim()); return out.slice(0, 4); }
  function hit(mx, my){ return Object.values(G.nodes).find(nd => Math.abs(mx - nd.x) <= nd.w / 2 && Math.abs(my - nd.y) <= nd.h / 2) || null; }
  function onMove(ev){ const r = G.cv.getBoundingClientRect(); const nd = hit(ev.clientX - r.left, ev.clientY - r.top); if (nd !== G.hover) { G.hover = nd; G.cv.style.cursor = nd ? 'pointer' : 'default'; draw(performance.now() / 1000); } }
  function onClick(ev){ const r = G.cv.getBoundingClientRect(); const nd = hit(ev.clientX - r.left, ev.clientY - r.top); if (!nd) return; if (nd.id === 'out') go('approvals'); else if (nd.id === 'desk') location.href = '/desk/ops'; else if (typeof lvFocusAgent === 'function') lvFocusAgent(nd.id); }
  function redraw(){ if (G.cv) { draw(performance.now() / 1000); wake(); } }
  return { mount, build, on, setCounts, redraw, resize, state: G };
})();
