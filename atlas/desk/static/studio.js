/* Design Studio: chat with the solutions designer -> live sketch of the agent hierarchy -> connect data -> launch.
   Depends on globals from index.html: $, api, esc, toast, go, loadDesks. */
const SPECIALIST_TOOLS = ['read_file', 'list_files', 'web_fetch', 'run_python', 'save_deliverable'];
const WF_COLORS = ['#0b5fcb', '#7c3aed', '#1f9d63', '#b45309'];
const KIND_LABEL = {smtp: 'EMAIL OUT', imap: 'INBOX', http: 'API', mcp: 'TOOL SERVER', webhook: 'WEB FORM'};
const TRIG_LABEL = {webhook: 'WEBHOOK', inbox: 'INBOX', schedule: 'SCHEDULE', manual: 'MANUAL'};
const DSG = {sid: null, bp: null, ready: false, stage: 1, deskId: null, plan: null, sel: null, els: new Map(), busy: false, tiers: [], mode: 'demo', saveT: null, jobs: []};

/* ---------------------------------------------------------------- entry */
async function openStudio(fresh) {
  document.querySelectorAll('main > section').forEach(s => s.classList.toggle('hide', s.id !== 'p-design'));
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('on', b.dataset.p === 'design'));
  if (location.hash !== '#design') history.replaceState(null, '', '#design');
  if (DSG.sid && !fresh) { renderCanvas(); return; }
  const T = await api('/templates'); if (!T) return;
  DSG.tiers = T.tiers; DSG.mode = T.mode;
  $('#ds-tier').innerHTML = T.tiers.map(t => `<option value="${t.id}">${esc(t.label)} — ${esc(t.note.split('.')[0])}</option>`).join('');
  $('#ds-modetag').textContent = T.mode === 'demo' ? 'demo designer' : 'live model';
  let S = null, prev = null;
  try { prev = fresh ? null : sessionStorage.getItem('ds_sid'); } catch (e) {}
  if (prev) { S = await fetch(`/api/design/${prev}`).then(r => r.ok ? r.json() : null).catch(() => null); }   // survive a page refresh
  if (!S) { S = await api('/design/start', {method: 'POST', body: {tier: $('#ds-tier').value}}); if (!S) return; }
  try { sessionStorage.setItem('ds_sid', S.sid); } catch (e) {}
  Object.assign(DSG, {sid: S.sid, bp: null, ready: !!S.ready, stage: 1, deskId: S.desk_id || null, plan: null, sel: null, busy: false, jobs: []});
  DSG.els.clear();
  $('#ds-svg').innerHTML = ''; $('#cv-empty').classList.remove('hide');
  $('#ds-chatwrap').classList.remove('hide'); $('#ds-connwrap').classList.add('hide');
  $('#ds-approve').classList.remove('hide'); $('#ds-approve').textContent = 'Approve & build';
  $('#ds-msgs').innerHTML = '';
  for (const m of S.transcript) addMsg(m.role === 'user' ? 'u' : 'a', m.text);
  setSugg(S.suggestions);
  setStage(1);
  $('#ds-approve').disabled = true;
  if (S.blueprint && (S.blueprint.agents || []).length) { DSG.bp = S.blueprint; setStage(2); renderCanvas(); $('#ds-approve').disabled = false; $('#ds-approve').classList.toggle('p', DSG.ready); }
  if (S.desk_id) {                                     // already built: jump straight to the connect stage
    const P = await api(`/design/${S.sid}/connect`);
    if (P && !P.error) { DSG.plan = P; setStage(3); $('#ds-chatwrap').classList.add('hide'); $('#ds-connwrap').classList.remove('hide'); $('#ds-approve').classList.add('hide'); renderConnect(); }
  }
  $('#ds-input').focus();
}

function setStage(n) {
  DSG.stage = n;
  document.querySelectorAll('#ds-steps div').forEach((d, i) => { d.classList.toggle('on', i + 1 === n); d.classList.toggle('done', i + 1 < n); });
}

/* ---------------------------------------------------------------- chat */
function addMsg(role, text) {
  const d = document.createElement('div'); d.className = 'msg ' + role;
  d.innerHTML = (role === 'a' ? '<span class="who">Designer</span>' : '') + '<span class="tx"></span>';
  d.querySelector('.tx').textContent = text || '';
  $('#ds-msgs').appendChild(d); $('#ds-msgs').scrollTop = 1e9; return d;
}
function setSugg(list) {
  $('#ds-sugg').innerHTML = (list || []).map(s => `<button data-s="${esc(s)}" onclick="dsSend(this.dataset.s)">${esc(s)}</button>`).join('');
}
function setStatus(node, text) {
  let s = node.querySelector('.st');
  if (!text) { if (s) s.remove(); return; }
  if (!s) { s = document.createElement('span'); s.className = 'st'; node.appendChild(s); }
  s.textContent = text; $('#ds-msgs').scrollTop = 1e9;
}
function dsKey(ev) { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); dsSend(); } }

async function dsSend(text) {
  if (DSG.busy || !DSG.sid) return;
  text = (text ?? $('#ds-input').value).trim(); if (!text) return;
  if (/^approve/i.test(text) && DSG.ready) return approveBuild();
  $('#ds-input').value = '';
  addMsg('u', text); setSugg([]);
  const node = addMsg('a', ''); node.classList.add('typing'); const tx = node.querySelector('.tx');
  DSG.busy = true; $('#ds-sendbtn').disabled = true;
  try {
    const r = await fetch(`/api/design/${DSG.sid}/say`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const rd = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const {value, done} = await rd.read(); if (done) break;
      buf += dec.decode(value, {stream: true});
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const ev = JSON.parse(line.slice(6));
          if (ev.t === 'tok') { tx.textContent = (tx.textContent + ev.d).replace(/\*\*/g, ''); $('#ds-msgs').scrollTop = 1e9; }
          else if (ev.t === 'status') { setStatus(node, ev.d); }
          else if (ev.t === 'done') { setStatus(node, ''); try { onDesignTurn(ev, tx); } catch (e) { console.error('design turn render failed', e); tx.textContent = ev.text; } }
          else if (ev.t === 'error') { tx.textContent = 'Designer error: ' + ev.error; }
        }
      }
    }
  } catch (e) { tx.textContent = (tx.textContent || '') + `\n[connection error: ${e.message}]`; }
  node.classList.remove('typing'); DSG.busy = false; $('#ds-sendbtn').disabled = false; $('#ds-input').focus();
}

function onDesignTurn(ev, tx) {
  tx.textContent = ev.text;
  setSugg(ev.suggestions);
  DSG.ready = !!ev.ready;
  if (ev.blueprint && (ev.blueprint.agents || []).length) {
    DSG.bp = ev.blueprint;
    if (DSG.stage < 2) setStage(2);
    renderCanvas();
  }
  $('#ds-approve').disabled = !(DSG.bp && DSG.bp.agents.length);
  $('#ds-approve').classList.toggle('p', DSG.ready);
  if (DSG.ready) $('#ds-readyhint').textContent = 'Blueprint ready — review the sketch, then approve.';
}

/* ---------------------------------------------------------------- layout */
function layout(bp, W) {
  const agents = bp.agents || [], wfs = bp.workflows || [], cons = bp.connectors || [];
  const trigs = wfs.map(w => ({...w.trigger, wf: w.id, name: w.name})).filter(t => t.kind && t.kind !== 'manual');
  const AW = 176, AH = 66, GAP = 26, CHIP_H = 24, COL = 160;
  const rowW = agents.length * AW + Math.max(0, agents.length - 1) * GAP;
  const sideW = (trigs.length ? COL + 40 : 0) + (cons.length ? COL + 40 : 0);
  const vw = Math.max(rowW + sideW + 120, 720);   // content-sized: the SVG scales up to fill the canvas width
  const cx = vw / 2;
  const nodes = [], edges = [], lanes = [];
  const atlas = {key: 'atlas', type: 'atlas', x: cx - 110, y: 36, w: 220, h: 62, data: {id: 'atlas', name: 'Atlas', role: 'Orchestrator · approvals · CRM'}};
  nodes.push(atlas);
  trigs.forEach((t, i) => {
    const n = {key: 'trig:' + t.wf, type: 'trig', x: 24, y: 36 + i * 66, w: COL, h: 50, data: t};
    nodes.push(n); edges.push({key: 'e:' + n.key, cls: 'edge trig', d: elbow(n.x + n.w, n.y + n.h / 2, atlas.x, atlas.y + atlas.h / 2)});
  });
  cons.forEach((c, i) => {
    const n = {key: 'conn:' + c.kind + ':' + i, type: 'conn', x: vw - 24 - COL, y: 36 + i * 66, w: COL, h: 50, data: c, idx: i};
    nodes.push(n); edges.push({key: 'e:' + n.key, cls: 'edge conn', d: elbow(n.x, n.y + n.h / 2, atlas.x + atlas.w, atlas.y + atlas.h / 2)});
  });
  const rowY = 190, startX = cx - rowW / 2;
  const pos = {};
  agents.forEach((a, i) => {
    const n = {key: 'ag:' + a.id, type: 'agent', x: startX + i * (AW + GAP), y: rowY, w: AW, h: AH + CHIP_H, data: a, idx: i};
    nodes.push(n); pos[a.id] = n;
    edges.push({key: 'e:' + n.key, cls: 'edge', d: `M${atlas.x + atlas.w / 2},${atlas.y + atlas.h} C${atlas.x + atlas.w / 2},${rowY - 40} ${n.x + AW / 2},${rowY - 40} ${n.x + AW / 2},${rowY}`});
  });
  let laneY = rowY + AH + CHIP_H + 42;
  wfs.forEach((w, k) => {
    const pts = w.steps.map(s => pos[s]).filter(Boolean).map(n => n.x + n.w / 2);
    if (!pts.length) return;
    lanes.push({key: 'wf:' + w.id, y: laneY, color: WF_COLORS[k % WF_COLORS.length], name: w.name, pts, x0: startX - 6});
    laneY += 32;
  });
  const H = Math.max(laneY + 20, 36 + trigs.length * 66 + 20, 36 + cons.length * 66 + 20, 420);
  return {nodes, edges, lanes, vw, vh: H};
}
function elbow(x1, y1, x2, y2) { const mx = (x1 + x2) / 2; return `M${x1},${y1} H${mx} V${y2} H${x2}`; }

/* ---------------------------------------------------------------- render (keyed; new elements sketch in) */
const SVG = 'http://www.w3.org/2000/svg';
function el(tag, attrs, cls) { const e = document.createElementNS(SVG, tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); if (cls) e.setAttribute('class', cls); return e; }
function txt(x, y, s, cls) { const t = el('text', {x, y}, cls); t.textContent = s; return t; }
function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function renderCanvas() {
  const svg = $('#ds-svg'), wrap = $('#ds-cv');
  if (!DSG.bp || !(DSG.bp.agents || []).length) { $('#cv-empty').classList.remove('hide'); return; }
  $('#cv-empty').classList.add('hide');
  const L = layout(DSG.bp, wrap.clientWidth || 900);
  svg.setAttribute('viewBox', `0 0 ${L.vw} ${L.vh}`);
  svg.style.height = Math.max(L.vh * ((wrap.clientWidth || 900) / L.vw), 420) + 'px';
  if (!svg.querySelector('defs')) {
    const d = el('defs'); d.innerHTML = '<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arrow"/></marker>';
    svg.appendChild(d);
    svg.appendChild(el('g', {id: 'g-edges'})); svg.appendChild(el('g', {id: 'g-lanes'})); svg.appendChild(el('g', {id: 'g-nodes'}));
  }
  const seen = new Set(); let stagger = 0;
  const delay = () => (stagger++ * 0.12).toFixed(2) + 's';
  // edges
  for (const e of L.edges) {
    seen.add(e.key);
    let p = DSG.els.get(e.key);
    if (!p) { p = el('path', {d: e.d, pathLength: 1}, e.cls + ' sk'); p.style.setProperty('--d', delay()); p.addEventListener('animationend', () => p.classList.remove('sk'), {once: true}); svg.querySelector('#g-edges').appendChild(p); DSG.els.set(e.key, p); }
    else { p.setAttribute('d', e.d); p.setAttribute('class', e.cls); }
  }
  // lanes (workflow order)
  for (const ln of L.lanes) {
    seen.add(ln.key);
    let g = DSG.els.get(ln.key);
    const fresh = !g;
    if (fresh) { g = el('g', {}, 'lane'); svg.querySelector('#g-lanes').appendChild(g); DSG.els.set(ln.key, g); }
    g.innerHTML = '';
    const lab = txt(ln.x0 - 4, ln.y + 4, clip(ln.name, 28).toUpperCase(), 'wflabel'); lab.setAttribute('text-anchor', 'end'); lab.setAttribute('fill', ln.color); g.appendChild(lab);
    if (ln.pts.length > 1) {
      const p = el('path', {d: `M${Math.min(...ln.pts)},${ln.y} L${Math.max(...ln.pts)},${ln.y}`, pathLength: 1, 'marker-end': 'url(#arr)'}, 'edge wf' + (fresh ? ' sk' : ''));
      p.style.stroke = ln.color; if (fresh) p.style.setProperty('--d', delay()); g.appendChild(p);
    }
    ln.pts.forEach((x, i) => {
      const n = el('g', {}, 'wfnum' + (fresh ? ' sk-txt' : '')); if (fresh) n.style.setProperty('--d', delay());
      const c = el('circle', {cx: x, cy: ln.y, r: 9}); c.style.stroke = ln.color; n.appendChild(c);
      const t = txt(x, ln.y + 3.5, String(i + 1)); t.setAttribute('text-anchor', 'middle'); t.setAttribute('fill', ln.color); n.appendChild(t);
      g.appendChild(n);
    });
  }
  // nodes
  for (const n of L.nodes) {
    seen.add(n.key);
    let g = DSG.els.get(n.key);
    const fresh = !g;
    if (fresh) { g = el('g', {}, 'node'); g.style.setProperty('--d', delay()); svg.querySelector('#g-nodes').appendChild(g); DSG.els.set(n.key, g); }
    g.setAttribute('class', 'node ' + n.type + (DSG.sel === n.key ? ' sel' : '') + (n.type === 'conn' && n.data.status === 'ok' ? ' ok' : ''));
    g.style.transform = `translate(${n.x}px,${n.y}px)`;
    g.onclick = ev => { ev.stopPropagation(); selectNode(n.key); };
    g.innerHTML = '';
    const sk = fresh ? ' sk' : '', skf = fresh ? ' sk-fill' : '', skt = fresh ? ' sk-txt' : '';
    const box = el('rect', {x: 0, y: 0, width: n.w, height: n.h, pathLength: 1}, 'box' + sk + skf);
    box.addEventListener('animationend', () => box.classList.remove('sk'), {once: true});
    g.appendChild(box);
    if (n.type === 'atlas') {
      g.appendChild(el('rect', {x: 0, y: 0, width: 4, height: n.h, fill: '#0b5fcb'}, 'bar' + skt));
      g.appendChild(txt(16, 26, 'Atlas', skt.trim()));
      g.appendChild(txt(16, 45, n.data.role, 'role' + skt));
      g.appendChild(txt(n.w - 12, 18, 'ORCHESTRATOR', 'badge' + skt)).setAttribute('text-anchor', 'end');
    } else if (n.type === 'agent') {
      const a = n.data;
      g.appendChild(el('rect', {x: 0, y: 0, width: 4, height: n.h, fill: a.color || '#7c3aed'}, 'bar' + skt));
      g.appendChild(txt(16, 24, clip(a.name, 20), skt.trim()));
      g.appendChild(txt(16, 42, clip(a.role, 30), 'role' + skt));
      if (a.strong) { const b = txt(n.w - 10, 16, 'STRONG', 'badge' + skt); b.setAttribute('text-anchor', 'end'); b.setAttribute('fill', '#b45309'); g.appendChild(b); }
      let cx = 12; const cy = 54;
      for (const t of (a.tools || []).slice(0, 4)) {
        const label = t.replace('_', ' '); const w = label.length * 5.6 + 10; if (cx + w > n.w - 8) break;
        const c = el('g', {}, 'chip' + skt); c.appendChild(el('rect', {x: cx, y: cy, width: w, height: 16})); c.appendChild(txt(cx + 5, cy + 11.5, label)); g.appendChild(c); cx += w + 4;
      }
    } else if (n.type === 'trig') {
      g.appendChild(txt(12, 18, TRIG_LABEL[n.data.kind] || n.data.kind.toUpperCase(), 'k' + skt));
      g.appendChild(txt(12, 36, clip(n.data.detail || n.data.name, 24), 'role' + skt));
    } else if (n.type === 'conn') {
      g.appendChild(txt(12, 18, KIND_LABEL[n.data.kind] || n.data.kind.toUpperCase(), 'k' + skt));
      g.appendChild(txt(12, 36, clip(n.data.name, 22), 'role' + skt));
      const dot = el('circle', {cx: n.w - 12, cy: 12, fill: n.data.status === 'ok' ? '#1f9d63' : (n.data.status === 'error' ? '#fb7185' : '#3a2c5e')}, 'dot');
      g.appendChild(dot);
      if (!n.data.required) { const b = txt(n.w - 22, 16, 'OPTIONAL', 'badge' + skt); b.setAttribute('text-anchor', 'end'); g.appendChild(b); }
    }
  }
  for (const [k, e] of [...DSG.els]) if (!seen.has(k)) { e.remove(); DSG.els.delete(k); }
}
window.addEventListener('resize', () => { if (DSG.bp && DSG.stage >= 2) renderCanvas(); });

/* ---------------------------------------------------------------- inspector */
function selectNode(key) {
  DSG.sel = key; renderCanvas();
  const I = $('#ds-insp'); I.classList.add('on');
  const bp = DSG.bp;
  if (key === 'atlas') {
    I.innerHTML = `<h3><i style="background:#0b5fcb"></i>Atlas${xbtn()}</h3><p class="hint" style="margin:6px 0 10px">Orchestrator. Plans each task, delegates to the specialists, reviews their work, updates the CRM and queues every outbound message for your approval. Always present; configured by Atlas Ops.</p>
      <label>Tools</label><div class="md" style="font-size:12px;color:var(--muted)">delegate · crm_lookup · crm_update · queue_action · http_request · schedule_task · mcp · run_python · remember · recall</div>
      <label>Specialists</label><div class="md" style="font-size:12px;color:var(--muted)">${esc(bp.agents.map(a => a.name).join(' · '))}</div>`;
    return;
  }
  if (key.startsWith('ag:')) {
    const a = bp.agents.find(x => 'ag:' + x.id === key); if (!a) return;
    I.innerHTML = `<h3><i style="background:${a.color}"></i>${esc(a.name)}${xbtn()}</h3>
      <label>Name</label><input value="${esc(a.name)}" oninput="agEdit('${a.id}','name',this.value)">
      <label>Role</label><input value="${esc(a.role)}" oninput="agEdit('${a.id}','role',this.value)">
      <label>Goal &amp; quality bar</label><textarea style="min-height:110px" oninput="agEdit('${a.id}','goal',this.value)">${esc(a.goal || '')}</textarea>
      <label>Tools</label><div class="tools">${SPECIALIST_TOOLS.map(t => `<label><input type="checkbox" ${a.tools.includes(t) ? 'checked' : ''} onchange="agTool('${a.id}','${t}',this.checked)">${t.replace('_', ' ')}</label>`).join('')}</div>
      <label>Model</label><label style="display:flex;gap:8px;align-items:center;margin:2px 0 0;color:var(--text);font-weight:600"><input type="checkbox" style="width:auto" ${a.strong ? 'checked' : ''} onchange="agEdit('${a.id}','strong',this.checked)"> Strong model for this agent (on the Best tier)</label>
      <div class="foot"><button class="btn sm d" onclick="agRemove('${a.id}')">Remove agent</button></div>`;
    return;
  }
  if (key.startsWith('trig:')) {
    const w = bp.workflows.find(x => 'trig:' + x.id === key); if (!w) return;
    I.innerHTML = `<h3><i style="background:var(--blue)"></i>Trigger · ${esc(w.name)}${xbtn()}</h3>
      <label>Starts when</label><select onchange="wfEdit('${w.id}','kind',this.value)">${['webhook', 'inbox', 'schedule', 'manual'].map(k => `<option value="${k}" ${w.trigger.kind === k ? 'selected' : ''}>${k}</option>`).join('')}</select>
      <label>Detail</label><input value="${esc(w.trigger.detail || '')}" oninput="wfEdit('${w.id}','detail',this.value)">
      <label>Steps (in order)</label><div class="md" style="font-size:12px;color:var(--muted)">${esc(w.steps.join(' → '))}</div>
      <p class="hint" style="margin-top:10px">webhook = web form / Zapier POST to the desk URL · inbox = IMAP watcher · schedule = recurring automation · manual = you start it from Leads.</p>`;
    return;
  }
  if (key.startsWith('conn:')) {
    const i = +key.split(':')[2]; const c = bp.connectors[i]; if (!c) return;
    I.innerHTML = `<h3><i style="background:var(--ok)"></i>${esc(c.name)}${xbtn()}</h3>
      <label>Type</label><div class="md" style="font-size:12px;color:var(--muted)">${esc(KIND_LABEL[c.kind] || c.kind)}</div>
      <label>Purpose</label><input value="${esc(c.purpose || '')}" oninput="cnEdit(${i},'purpose',this.value)">
      <label style="display:flex;gap:8px;align-items:center;color:var(--text);font-weight:600"><input type="checkbox" style="width:auto" ${c.required ? 'checked' : ''} onchange="cnEdit(${i},'required',this.checked)"> Required before launch</label>
      <p class="hint" style="margin-top:10px">Credentials are entered in the Connect step, after the desk is built. Nothing is stored until then.</p>
      <div class="foot"><button class="btn sm d" onclick="cnRemove(${i})">Remove</button></div>`;
  }
}
function xbtn() { return '<button class="x" onclick="closeInsp()" title="Close">×</button>'; }
function closeInsp() { DSG.sel = null; $('#ds-insp').classList.remove('on'); renderCanvas(); }
function bpChanged() { renderCanvas(); clearTimeout(DSG.saveT); DSG.saveT = setTimeout(() => api(`/design/${DSG.sid}/blueprint`, {method: 'POST', body: {blueprint: DSG.bp}}), 500); }
function agEdit(id, k, v) { const a = DSG.bp.agents.find(x => x.id === id); if (!a) return; a[k] = v; bpChanged(); }
function agTool(id, t, on) { const a = DSG.bp.agents.find(x => x.id === id); if (!a) return; a.tools = on ? [...new Set([...a.tools, t])] : a.tools.filter(x => x !== t); bpChanged(); }
function agRemove(id) { DSG.bp.agents = DSG.bp.agents.filter(x => x.id !== id); DSG.bp.workflows.forEach(w => w.steps = w.steps.filter(s => s !== id)); closeInsp(); bpChanged(); }
function agAdd() {
  if (!DSG.bp) DSG.bp = {business: {}, agents: [], workflows: [], connectors: [], policy: {no_money_figures: true, max_words: 220, banned_phrases: []}};
  const n = DSG.bp.agents.length + 1; const id = 'agent_' + n;
  DSG.bp.agents.push({id, name: 'New agent ' + n, role: 'Specialist', goal: '', tools: ['read_file', 'list_files'], reports_to: 'atlas', strong: false, color: ['#7c3aed', '#db2777', '#1f9d63', '#b45309', '#0e7490', '#6d28d9'][(n - 1) % 6]});
  if (DSG.bp.workflows[0]) DSG.bp.workflows[0].steps.push(id);
  $('#cv-empty').classList.add('hide'); if (DSG.stage < 2) setStage(2); $('#ds-approve').disabled = false;
  bpChanged(); selectNode('ag:' + id);
}
function wfEdit(id, k, v) { const w = DSG.bp.workflows.find(x => x.id === id); if (!w) return; w.trigger[k] = v; bpChanged(); }
function cnEdit(i, k, v) { if (DSG.bp.connectors[i]) { DSG.bp.connectors[i][k] = v; bpChanged(); } }
function cnRemove(i) { DSG.bp.connectors.splice(i, 1); closeInsp(); bpChanged(); }

/* ---------------------------------------------------------------- build + connect */
async function approveBuild() {
  if (!DSG.bp || !DSG.bp.agents.length) return toast('Nothing to build yet');
  $('#ds-approve').disabled = true; $('#ds-approve').textContent = 'Building…';
  const r = await api(`/design/${DSG.sid}/build`, {method: 'POST', body: {blueprint: DSG.bp, tier: $('#ds-tier').value}});
  $('#ds-approve').textContent = 'Approve & build';
  if (!r || r.error) { $('#ds-approve').disabled = false; return toast(r?.error || 'Build failed'); }
  DSG.deskId = r.desk.id; DSG.plan = r.connect; DSG.jobs = r.jobs || [];
  toast(`Desk "${r.desk.business_name || r.desk.name}" built`);
  setStage(3); closeInsp();
  $('#ds-chatwrap').classList.add('hide'); $('#ds-connwrap').classList.remove('hide');
  $('#ds-approve').classList.add('hide'); $('#ds-readyhint').textContent = 'Desk built. Connect your data on the left — every connector tests live.';
  renderConnect();
  if (typeof loadCfg === 'function') loadCfg();   // the portal now points at the new desk: refresh roster, workflows, names
}

function renderConnect() {
  const P = DSG.plan; const list = $('#cn-list');
  const items = P.connectors;
  const sec = ['password', 'token', 'secret', 'env'];
  list.innerHTML = items.length ? items.map((c, i) => {
    const st = c.status === 'ok' ? 'ok' : (c.status === 'error' ? 'err' : '');
    let body = '';
    if (c.kind === 'webhook') {
      body = `<div class="hint">${esc(c.hint)}</div><label>Desk hook URL</label><div class="row" style="margin-top:0"><input readonly value="${esc(P.hook_url)}" onclick="this.select()"><button class="btn sm" onclick="navigator.clipboard.writeText('${esc(P.hook_url)}');toast('Copied')">Copy</button></div>
        <div class="row"><button class="btn sm p" onclick="cnTestHook(${i})">Send a test lead</button><span class="hint" style="margin:0">Posts a sample enquiry to the URL — a run starts immediately.</span></div><div class="out" id="cn-out-${i}"></div>`;
    } else {
      body = `<div class="hint">${esc(c.hint)}</div>` + c.fields.map(f => `<label>${esc(f)}</label><input id="cn-${i}-${f}" type="${sec.some(s => f.includes(s)) ? 'password' : 'text'}" placeholder="${esc(cnPlaceholder(c.kind, f))}" autocomplete="off">`).join('') +
        `<div class="row"><button class="btn sm p" onclick="cnSave(${i})">Connect &amp; test</button><label style="margin:0;display:flex;gap:6px;align-items:center;font-size:12px"><input type="checkbox" id="cn-${i}-auto" style="width:auto"> writes without approval</label></div><div class="out" id="cn-out-${i}"></div>`;
    }
    return `<div class="cn ${st} ${i === 0 ? 'open' : ''}" id="cn-${i}"><div class="h" onclick="this.parentElement.classList.toggle('open')"><b>${esc(c.name)}</b><small>${esc(KIND_LABEL[c.kind] || c.kind)}${c.purpose ? ' · ' + esc(c.purpose) : ''}${c.required ? '' : ' · optional'}</small><span class="st"><i></i>${esc(c.status || 'not connected')}</span></div><div class="b">${body}</div></div>`;
  }).join('') : '<div class="empty">This desk needs no external connections. You can launch it now.</div>';
  const jobs = DSG.jobs.length ? `<div class="hint" style="padding:0 20px 8px">Automations scheduled: ${DSG.jobs.map(j => esc(j.name)).join(', ')} — manage them under Automations.</div>` : '';
  $('#cn-jobs').innerHTML = jobs;
  DSG.bp.connectors.forEach((c, i) => { c.status = items[i]?.status; });
  renderCanvas();
}
function cnPlaceholder(kind, f) {
  const P = {smtp: {host: 'smtp.gmail.com', port: '587', user: 'you@yourdomain.com', password: 'app password', from_email: 'you@yourdomain.com', from_name: 'Your name, Company'},
    imap: {host: 'imap.gmail.com', port: '993', user: 'you@yourdomain.com', password: 'app password', folder: 'INBOX'},
    http: {base_url: 'https://api.example.com', auth_type: 'bearer | header | query | basic | none', token: 'API key', headers: '{"X-Api-Version":"2"}', notes: 'What the API does, which paths matter'},
    mcp: {command: 'npx -y @modelcontextprotocol/server-github', env: 'GITHUB_PERSONAL_ACCESS_TOKEN=...', notes: 'What this server is for'}};
  return (P[kind] || {})[f] || '';
}
async function cnSave(i) {
  const c = DSG.plan.connectors[i]; const cfg = {};
  for (const f of c.fields) { const v = $(`#cn-${i}-${f}`).value.trim(); if (v) cfg[f] = v; }
  const auto = $(`#cn-${i}-auto`).checked;
  const out = $(`#cn-out-${i}`); out.textContent = 'Connecting…';
  let r;
  if (c.connector_id) r = await api(`/connectors/${c.connector_id}`, {method: 'PATCH', body: {config: cfg, auto}});
  else r = await api('/connectors', {method: 'POST', body: {kind: c.kind, name: c.name, config: cfg, auto}});
  if (!r || r.error) { out.textContent = r?.error || 'failed'; return; }
  c.connector_id = r.id;
  const t = await api(`/connectors/${r.id}/test`, {method: 'POST', body: {}});
  const ok = t && !t.error && (t.ok !== false) && !/^error/i.test(t.result || '');
  out.textContent = t?.result || t?.error || JSON.stringify(t);
  c.status = ok ? 'ok' : 'error';
  const card = $(`#cn-${i}`); card.classList.toggle('ok', ok); card.classList.toggle('err', !ok); card.querySelector('.st').innerHTML = `<i></i>${ok ? 'connected' : 'error'}`;
  DSG.bp.connectors[i].status = c.status; renderCanvas();
  if (ok) toast(`${c.name} connected`);
}
async function cnTestHook(i) {
  const out = $(`#cn-out-${i}`); out.textContent = 'Posting test lead…';
  const r = await fetch(DSG.plan.hook_url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'Test Lead', email: 'test.lead@example.com', company: 'Design Studio', source: 'webhook test', notes: 'Hello — I found you online and would like to know how you can help. Please get back to me with next steps.'})});
  const j = await r.json().catch(() => ({}));
  out.textContent = JSON.stringify(j);
  const ok = r.ok; const c = DSG.plan.connectors[i]; c.status = ok ? 'ok' : 'error'; DSG.bp.connectors[i].status = c.status;
  const card = $(`#cn-${i}`); card.classList.toggle('ok', ok); card.querySelector('.st').innerHTML = `<i></i>${ok ? 'receiving' : 'error'}`;
  renderCanvas();
  if (ok && j.run_id) { toast('Test lead received — run started'); liveWant = j.run_id; }
}
async function dsTestLead() { setStage(4); toast('Seeding sample leads…'); const r = await api('/demo/seed', {method: 'POST', body: {}}); if (r && r[0]) liveWant = r[0].run_id; location.hash = '#live'; location.reload(); }
function dsLaunch() { setStage(4); location.hash = '#dash'; location.reload(); }
