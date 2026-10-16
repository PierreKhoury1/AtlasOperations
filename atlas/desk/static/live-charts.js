/* Live desk charts: small canvases under the board, all from the run's own events, redrawn as they arrive.
   LVC.render(S) with S = liveState (series filled by live.js). */
const LVC = (() => {
  const dpr = () => Math.min(2, window.devicePixelRatio || 1);
  const INK = '#E6ECF7', MUTED = '#8FA0BA', FAINT = '#5A6A85', GRID = 'rgba(255,255,255,.07)';
  const TYPE_COLORS = { person: '#5B93F0', organisation: '#7CC4FF', place: '#34C8A5', invoice: '#E0A33A', booking: '#B98BFF', order: '#F08AB4', product: '#9AD36A', camera: '#7FB5FF', case: '#F26B5B', message: '#E0A33A', crm: '#5B93F0', file: '#C7D2E3', memory: '#A78BFA' };
  const TOOL_COLORS = { record_find: '#5B93F0', record_get: '#7CC4FF', record_save: '#34C8A5', case_open: '#F26B5B', case_update: '#F59E8B', case_list: '#FBBF9A', queue_action: '#E0A33A', crm_lookup: '#8FD3FF', crm_update: '#4ADE80', delegate: '#C084FC', remember: '#A78BFA', recall: '#C4B5FD', read_file: '#C7D2E3', list_files: '#94A3B8', run_python: '#F472B6', save_deliverable: '#38BDF8', finish: '#22C55E' };
  const SHORT = { record_find: 'find', record_get: 'get', record_save: 'save', case_open: 'open case', case_update: 'update case', case_list: 'list cases', queue_action: 'queue msg', crm_lookup: 'crm lookup', crm_update: 'crm update', save_deliverable: 'deliverable', calendar_free_slots: 'free slots', calendar_book: 'book', list_files: 'files', read_file: 'read', run_python: 'python', generate_media: 'media', camera_look: 'look', camera_events: 'cam events', camera_ask: 'ask cams' };
  const col = (name, i) => TOOL_COLORS[name] || ['#60A5FA', '#34D399', '#FBBF24', '#F472B6', '#A78BFA', '#38BDF8', '#F87171', '#4ADE80'][i % 8];

  function cv(id){ const c = document.getElementById(id); if (!c) return null; const r = c.parentElement.getBoundingClientRect(); const w = Math.max(60, r.width - 24), h = Math.max(40, c.dataset.h ? +c.dataset.h : 96);
    if (c._w !== w || c._h !== h) { c._w = w; c._h = h; c.width = w * dpr(); c.height = h * dpr(); c.style.width = w + 'px'; c.style.height = h + 'px'; }
    const x = c.getContext('2d'); x.setTransform(dpr(), 0, 0, dpr(), 0, 0); x.clearRect(0, 0, w, h); return { x, w, h }; }
  const font = (x, s, wgt) => { x.font = `${wgt || 400} ${s}px Inter, system-ui, sans-serif`; };
  const mono = (x, s) => { x.font = `500 ${s}px "IBM Plex Mono", Consolas, monospace`; };
  const empty = (k, txt) => { font(k.x, 11.5); k.x.fillStyle = FAINT; k.x.textAlign = 'center'; k.x.fillText(txt, k.w / 2, k.h / 2 + 4); k.x.textAlign = 'left'; };

  /* ---- tool mix donut + legend */
  function mix(S){ const k = cv('lvc-mix'); if (!k) return; const T = S.series.tools, names = Object.keys(T).sort((a, b) => T[b] - T[a]); const total = names.reduce((n, a) => n + T[a], 0);
    if (!total) return empty(k, 'tool calls appear here');
    const cx = 48, cy = k.h / 2, R = Math.min(40, k.h / 2 - 6), r = R * .6; let a0 = -Math.PI / 2;
    names.forEach((n, i) => { const a1 = a0 + T[n] / total * Math.PI * 2; k.x.beginPath(); k.x.arc(cx, cy, R, a0, a1); k.x.arc(cx, cy, r, a1, a0, true); k.x.closePath(); k.x.fillStyle = col(n, i); k.x.fill(); a0 = a1 + .012; });
    font(k.x, 15, 600); k.x.fillStyle = INK; k.x.textAlign = 'center'; k.x.fillText(String(total), cx, cy + 5); k.x.textAlign = 'left';
    const lx = cx + R + 14, lw = k.w - lx - 30; let ly = 14; names.slice(0, 6).forEach((n, i) => { k.x.fillStyle = col(n, i); k.x.fillRect(lx, ly - 7, 8, 8); mono(k.x, 10.5); k.x.fillStyle = INK; k.x.fillText(clip(k.x, SHORT[n] || n, lw), lx + 13, ly); k.x.fillStyle = MUTED; k.x.textAlign = 'right'; k.x.fillText(String(T[n]), k.w, ly); k.x.textAlign = 'left'; ly += 14; }); }

  /* ---- load per agent: calls, approvals, writes as stacked bars; a dot for busy */
  function load(S){ const k = cv('lvc-load'); if (!k) return; const A = Object.values(S.agents).filter(a => a.tools || a.turns); if (!A.length) return empty(k, 'who is doing the work');
    const max = Math.max(1, ...A.map(a => a.tools)); const rowH = Math.min(22, (k.h - 6) / A.length), lw = 92;
    A.forEach((a, i) => { const y = 4 + i * rowH, bw = (k.w - lw - 36) * (a.tools / max); font(k.x, 11, 500); k.x.fillStyle = INK; k.x.fillText(clip(k.x, a.name, lw - 10), 0, y + rowH * .65);
      k.x.fillStyle = 'rgba(255,255,255,.06)'; rr(k.x, lw, y + 3, k.w - lw - 36, rowH - 7, 4); k.x.fill();
      const g = k.x.createLinearGradient(lw, 0, lw + bw, 0); g.addColorStop(0, a.color); g.addColorStop(1, shade(a.color, .55)); k.x.fillStyle = g; rr(k.x, lw, y + 3, Math.max(2, bw), rowH - 7, 4); k.x.fill();
      const ap = S.out.approvals.filter(x => x.by === a.name).length; if (ap) { k.x.fillStyle = '#E0A33A'; for (let j = 0; j < Math.min(ap, 8); j++) { k.x.beginPath(); k.x.arc(lw + bw + 8 + j * 7, y + rowH / 2, 2.2, 0, Math.PI * 2); k.x.fill(); } }
      mono(k.x, 10.5); k.x.fillStyle = MUTED; k.x.textAlign = 'right'; k.x.fillText(String(a.tools), k.w, y + rowH * .65); k.x.textAlign = 'left';
      if (!a.done && a.status !== 'idle' && S.active) { k.x.fillStyle = a.color; k.x.beginPath(); k.x.arc(lw - 8, y + rowH / 2, 2.6 + Math.sin(performance.now() / 180) * 1, 0, Math.PI * 2); k.x.fill(); } }); }

  /* ---- activity over time: calls per bucket, stacked by agent, with approvals as ticks */
  function activity(S){ const k = cv('lvc-act'); if (!k) return; const ev = S.series.calls; const all = Object.values(ev).flat(); if (!all.length) return empty(k, 'activity over the run');
    const t0 = S.series.t0 || Math.min(...all), t1 = S.active && !LV.replaying ? Date.now() / 1000 : Math.max(...all, t0 + 1); const span = Math.max(10, t1 - t0), N = Math.min(60, Math.max(12, Math.floor(k.w / 7)));
    const bins = {}; let max = 1; const agents = Object.keys(ev);
    agents.forEach(a => { bins[a] = new Array(N).fill(0); ev[a].forEach(ts => { const i = Math.min(N - 1, Math.floor((ts - t0) / span * N)); bins[a][i]++; }); });
    for (let i = 0; i < N; i++) max = Math.max(max, agents.reduce((n, a) => n + bins[a][i], 0));
    const bw = k.w / N, base = k.h - 14;
    for (let i = 0; i < N; i++) { let y = base; agents.forEach(a => { const v = bins[a][i]; if (!v) return; const h = v / max * (base - 6); const c = (S.agents[a] || {}).color || '#60A5FA'; k.x.fillStyle = c; rr(k.x, i * bw + 1, y - h, Math.max(1.5, bw - 2), h, 2); k.x.fill(); y -= h; }); }
    S.out.approvals.forEach(x => { const px = (x.ts - t0) / span * k.w; k.x.fillStyle = '#E0A33A'; k.x.fillRect(px - 1, base + 3, 2, 6); });
    S.out.policy.forEach(x => { const px = (x.ts - t0) / span * k.w; k.x.fillStyle = '#F26B5B'; k.x.fillRect(px - 1, base + 3, 2, 6); });
    mono(k.x, 9.5); k.x.fillStyle = FAINT; k.x.fillText('start', 0, k.h - 1); k.x.textAlign = 'right'; k.x.fillText(fmt(span), k.w, k.h - 1); k.x.textAlign = 'left'; }

  /* ---- who touched what: agents x record types, heat */
  function matrix(S){ const k = cv('lvc-matrix'); if (!k) return; const M = S.series.matrix; const agents = Object.keys(M); const types = [...new Set(agents.flatMap(a => Object.keys(M[a])))];
    if (!agents.length || !types.length) return empty(k, 'who touched what: people, places, invoices, cases, messages');
    const narrow = k.w < 330, lw = narrow ? 64 : 92, th = 16, cellW = Math.min(64, (k.w - lw) / types.length), cellH = Math.min(20, (k.h - th - 2) / agents.length);
    const ABBR = { person: 'people', organisation: 'orgs', place: 'places', invoice: 'invoices', booking: 'bookings', order: 'orders', product: 'products', camera: 'cams', case: 'cases', message: 'msgs', memory: 'memory', file: 'files', record: 'records' };
    const TINY = { person: 'ppl', organisation: 'org', place: 'plc', invoice: 'inv', booking: 'bkg', order: 'ord', product: 'prd', camera: 'cam', case: 'case', message: 'msg', memory: 'mem', file: 'file', record: 'rec' };
    let max = 1; agents.forEach(a => types.forEach(t => max = Math.max(max, M[a][t] || 0)));
    mono(k.x, cellW < 40 ? 8.5 : 9.5); types.forEach((t, j) => { k.x.fillStyle = TYPE_COLORS[t] || MUTED; k.x.textAlign = 'center'; const lab = cellW < 44 ? (TINY[t] || t.slice(0, 3)) : (ABBR[t] || t); k.x.fillText(clip(k.x, lab, cellW - 3), lw + j * cellW + cellW / 2, 11); }); k.x.textAlign = 'left';
    agents.forEach((a, i) => { const y = th + i * cellH; font(k.x, 10.5, 500); k.x.fillStyle = INK; const nm = (S.agents[a] || {}).name || a; k.x.fillText(clip(k.x, narrow ? nm.split(/[\s&]+/).map(w => w[0]).join('').toUpperCase() + ' ' + nm.split(' ')[0] : nm, lw - 8), 0, y + cellH * .7);
      types.forEach((t, j) => { const v = M[a][t] || 0; const c = TYPE_COLORS[t] || '#60A5FA'; k.x.fillStyle = v ? rgba(c, .18 + .72 * (v / max)) : 'rgba(255,255,255,.04)'; rr(k.x, lw + j * cellW + 1, y + 1, cellW - 2, cellH - 2, 4); k.x.fill();
        if (v) { mono(k.x, 10); k.x.fillStyle = v / max > .5 ? '#0B1220' : INK; k.x.textAlign = 'center'; k.x.fillText(String(v), lw + j * cellW + cellW / 2, y + cellH * .7); k.x.textAlign = 'left'; } }); }); }

  /* ---- in-card sparkline for the board (world coords, caller sets transform) */
  function spark(x, a, S, px, py, w, h){ const ev = (S.series.calls[a.id] || []); if (!ev.length) return; const now = S.active && !LV.replaying ? Date.now() / 1000 : Math.max(...ev); const t0 = now - 90, N = 18, bins = new Array(N).fill(0); let max = 1;
    ev.forEach(ts => { if (ts < t0) return; const i = Math.min(N - 1, Math.floor((ts - t0) / 90 * N)); bins[i]++; max = Math.max(max, bins[i]); });
    const bw = w / N; for (let i = 0; i < N; i++) { const v = bins[i], bh = v ? 2 + (h - 2) * v / max : 1.5; x.fillStyle = v ? a.color : 'rgba(16,24,40,.12)'; x.fillRect(px + i * bw, py + h - bh, Math.max(1.5, bw - 1.5), bh); } }

  /* ---- helpers */
  function rr(x, X, Y, W, H, r){ r = Math.min(r, W / 2, H / 2); x.beginPath(); x.moveTo(X + r, Y); x.arcTo(X + W, Y, X + W, Y + H, r); x.arcTo(X + W, Y + H, X, Y + H, r); x.arcTo(X, Y + H, X, Y, r); x.arcTo(X, Y, X + W, Y, r); x.closePath(); }
  function clip(x, s, w){ s = String(s || ''); if (x.measureText(s).width <= w) return s; while (s.length > 1 && x.measureText(s + '…').width > w) s = s.slice(0, -1); return s + '…'; }
  function rgba(h, a){ const m = String(h).replace('#', ''); const v = m.length === 3 ? m.split('').map(c => c + c).join('') : m; const n = parseInt(v, 16); return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`; }
  function shade(h, f){ const m = String(h).replace('#', ''); const n = parseInt(m.length === 3 ? m.split('').map(c => c + c).join('') : m, 16); const r = Math.round(((n >> 16) & 255) * f + 255 * (1 - f)), g = Math.round(((n >> 8) & 255) * f + 255 * (1 - f)), b = Math.round((n & 255) * f + 255 * (1 - f)); return `rgb(${r},${g},${b})`; }
  const fmt = s => s < 60 ? s.toFixed(0) + 's' : Math.floor(s / 60) + 'm ' + String(Math.floor(s % 60)).padStart(2, '0') + 's';

  function render(S){ if (!S || !S.series) return; try { mix(S); load(S); activity(S); matrix(S); } catch (e) { console.warn('charts', e); } }
  return { render, spark, TYPE_COLORS, TOOL_COLORS };
})();
