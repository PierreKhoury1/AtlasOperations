/* Atlas Workspace: talk to Atlas -> the workspace opens -> Atlas assigns the team in front of you -> the structure
   morphs as you keep talking -> build -> give the team a job and watch every agent work, output streaming live.
   Everything on screen comes from real events (design turns, blueprint diffs, the run's SSE feed). */
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const api = async (p, opt) => { const r = await fetch('/api' + p, opt ? {headers:{'Content-Type':'application/json'}, ...opt, body: opt.body ? JSON.stringify(opt.body) : undefined} : undefined); if (r.status === 401) { location.href = '/login?next=/desk/workspace'; return null; } try { return await r.json(); } catch (_) { return {error: 'bad response ' + r.status}; } };
function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('on'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('on'), 2800); }

const PALETTE = ['#4c90f0', '#32a467', '#ec9a3c', '#9881f3', '#2ec4b6', '#e76a6e', '#68c1ee', '#d1980b', '#c274c2', '#8eb125'];   // muted, one per agent
const hhmmss = d => (d || new Date()).toISOString().slice(11, 19);   // UTC everywhere, like the clock and the activity log
function setState(t){ t = String(t || 'ready').toLowerCase(); const busy = t !== 'ready'; ['#sb-state', '#chat-state'].forEach(q => { const el = $(q); if (el) { el.textContent = t; el.classList.toggle('busy', busy); } }); }
function setDeskStatus(){
  const d = $('#sb-desk'); if (d) d.textContent = W.deskId ? `${W.deskName || 'desk'} #${W.deskId}` : (W.bp ? 'draft' : 'none');
  const free = W.tier === 'free'; const t = $('#sb-tier'); if (t) t.textContent = free ? 'free' : 'paid';
  const hm = $('#h-models'); if (hm) hm.textContent = free ? 'free tier' : 'paid tier';
  const tb = $('#tier-btn'); if (tb) tb.textContent = free ? 'free models' : 'paid models';
  const as = $('#as-sub'); if (as) as.textContent = free ? 'orchestrator · free models' : 'orchestrator · paid models';
}
async function initStatus(){
  const live = W.mode === 'live' && !W.liveReason;
  $('#sb-live').classList.toggle('hide', !live); $('#sb-demo').classList.toggle('hide', live); $('#sb-demo').textContent = (W.mode || 'demo').toUpperCase();
  const h = await api('/health') || {};
  const eng = h.hermes_agent ? 'hermes_agent' : (h.ok ? 'built-in' : 'unreachable');
  $('#sb-engine').textContent = eng; $('#sb-mode').textContent = eng + (live ? '' : ' · ' + (W.mode || 'demo'));
  $('#sb-dot').className = 'dot ' + (h.ok === false ? 'd-err' : (live ? 'd-ok' : 'd-warn'));
  setDeskStatus();
  const tick = () => { const d = new Date(); $('#sb-clock').textContent = d.toLocaleDateString('en-GB', {day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC'}) + ' · ' + d.toISOString().slice(11, 19) + ' UTC'; }; tick(); setInterval(tick, 1000);
}
const W = {
  phase: 'meet',            // meet -> design -> run
  sid: null, mode: 'demo', tier: 'free', liveReason: '',
  bp: null,                 // current blueprint (design phase)
  agents: new Map(),        // id -> {id,name,role,goal,instructions,tools,engine,reports_to,members,color}
  order: [],                // draw order
  pos: new Map(),           // id -> {x,y,w,h}
  deskId: null, deskName: '',
  run: null,                // {id, es, inst: Map(instId -> state), active}
  sel: null, busy: false, edgeAnim: null,
  cams: new Map(),          // name -> {name, id, source, sample, journal, alerts, el, seenTs, lastEv}
  camPoll: null, evSince: 0,
  manual: new Map(),        // id -> {x,y}: cards the owner dragged (canvas units)
  pan: {x: 0, y: 0}, uz: 1, // background pan (px) and the owner's zoom on top of the fit
  addedCams: new Map(),     // name -> source: feeds attached by hand before the desk is built
};

/* ------------------------------------------------------------------ boot */
window.addEventListener('DOMContentLoaded', boot);
async function boot(){
  const cfg = await api('/config') || {};
  W.mode = cfg.mode || 'demo'; W.liveReason = cfg.live_reason || '';
  initStatus();
  const q = new URLSearchParams(location.search);
  if (q.get('desk') && cfg.desk) {                                   // existing desk: open straight into run mode
    W.deskId = cfg.desk.id; W.deskName = cfg.business && cfg.business.name || cfg.desk.name; loadView();
    W.tier = cfg.desk.tier || 'free'; W.workflows = cfg.workflows || []; W.template = cfg.template || ''; setDeskStatus();
    loadAgentsFromConfig(cfg.agents || []);
    $('#bz-name').textContent = W.deskName; setDeskStatus();
    await openWorkspace();
    await spawnAll();
    const cams = await api('/cameras') || {};
    const list = (cams.cameras || []).map(c => ({name: c.name, id: c.id, source: (c.config || {}).source || '', journal: ['1','true','on','yes'].includes(String((c.config || {}).journal || '').toLowerCase()), alerts: !(c.rule && c.rule.alerts === false)}));
    if (list.length) await applyCams(list, true);
    enterRunMode();
    loadMyDesks();
    if (W.cams.size) { startCamPoll(); setSugg(CAM_QUESTIONS); }
    addMsg('a', W.cams.size
      ? `${W.deskName}: ${W.agents.size} agent${W.agents.size === 1 ? '' : 's'}, ${W.cams.size} camera${W.cams.size === 1 ? '' : 's'} keeping a journal. Ask about what the cameras saw, give the team a job in the bar above, or describe a change.`
      : `${W.deskName}: ${W.agents.size} agent${W.agents.size === 1 ? '' : 's'} on the desk. Give the team a job in the bar above, or describe a change.`);
    tutStart(false, W.cams.size ? 'watching' : 'built');
    return;
  }
  const s = await api('/design/start', {method: 'POST', body: {tier: W.tier}});
  if (!s || s.error) { addMsg('s', (s && s.error) || 'Atlas is not available right now.'); setState('unavailable'); return; }
  W.sid = s.sid; W.mode = s.mode; loadView();
  sessionStorage.setItem('ws_sid', s.sid);
  const greet = (s.transcript && s.transcript[0] && s.transcript[0].text) || 'Describe the business and the work to take on.';
  addMsg('a', greet);
  setSugg(s.suggestions || []);
  loadMyDesks(); renderAll();
  if (W.liveReason) addMsg('s', 'no model key: ' + W.liveReason + ' (running the scripted designer)');
  tutStart(false, 'meet');
}

function loadAgentsFromConfig(list){
  W.agents.clear(); W.order = [];
  list.forEach((a, i) => { if (a.id === 'atlas') return;
    W.agents.set(a.id, {id: a.id, name: a.name, role: a.role || '', goal: a.goal || '', instructions: a.instructions || [], tools: (a.tools || []).filter(t => !['delegate','list_agents','finish'].includes(t)),
      engine: a.engine || 'atlas', reports_to: a.reports_to || 'atlas', members: a.members || [], color: PALETTE[i % PALETTE.length]}); });
  W.order = orderIds();
}

/* ------------------------------------------------------------------ chat */
function addMsg(role, text, cls){ const d = document.createElement('div'); d.className = 'm ' + role + (cls ? ' ' + cls : ''); d.dataset.who = role === 'u' ? 'You' : (role === 'a' ? 'Atlas' : ''); d.dataset.t = hhmmss(); if (role === 'a') { d.classList.add('md'); d.innerHTML = md(text); } else d.textContent = text; $('#msgs').appendChild(d); $('#msgs').scrollTop = 1e9; return d; }
async function typeMsg(text){ return addMsg('a', text); }      // shown at once: no typing effect
function setSugg(list){ $('#sugg').innerHTML = (list || []).map(s => `<button onclick="send(${JSON.stringify(s).replace(/"/g, '&quot;')})">${esc(s)}</button>`).join(''); }
function sayKey(ev){ if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(); } }

async function send(text){
  text = (text || $('#say').value).trim(); if (!text || W.busy) return;
  $('#say').value = ''; W.typing = false;
  if (/^switch to the paid model/i.test(text) && W.sid) { await setTier('balanced'); return send(W.lastSaid || ''); }
  if (W.phase === 'run' && W.cams.size && (!W.sid || looksLikeQuestion(text))) return askCams(text);
  W.lastSaid = text;
  if (W.phase === 'run' && !W.sid) { $('#job').value = text; return deploy(); }
  addMsg('u', text); setSugg([]);
  if (W.phase === 'meet') $('#ws').classList.add('talk');               // the brief screen becomes a full conversation
  W.busy = true; $('#send').disabled = true; setState('working');
  const d = addMsg('a', ''); let acc = '', raf = 0;
  const paint = () => { raf = 0; d.innerHTML = md(acc) + '<i class="cur"></i>'; $('#msgs').scrollTop = 1e9; };
  paint();
  let result = null;
  try {
    const r = await fetch(`/api/design/${W.sid}/say`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream: true});
      let i; while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          let ev; try { ev = JSON.parse(line.slice(6)); } catch (_) { continue; }
          if (ev.t === 'tok') { acc += ev.d; if (!raf) raf = requestAnimationFrame(paint); }
          else if (ev.t === 'status') { setState(ev.d); }
          else if (ev.t === 'done') result = ev;
          else if (ev.t === 'error') result = {error: ev.error};
        }
      }
    }
  } catch (e) { result = {error: String(e)}; }
  cancelAnimationFrame(raf);
  W.busy = false; $('#send').disabled = false; setState('ready');
  if (!result || result.error) { d.innerHTML = md((result && result.error) || 'Atlas did not answer. Try again.'); return; }
  const last = (result.transcript || []).filter(m => m.role === 'assistant').pop();
  d.innerHTML = md(result.text || (last && last.text) || acc || 'Atlas did not answer. Try again.');   // the final text, without the machine block
  setSugg(result.suggestions || []);
  if (result.blueprint && (result.blueprint.agents || []).length) { await applyBlueprint(result.blueprint); setDeskStatus(); }
  $('#build-btn').disabled = !(W.bp && W.bp.agents && W.bp.agents.length);
  $('#build-hint').textContent = result.ready ? 'Draft complete. Build it, or keep refining.' : (W.bp ? 'Describe changes to reshape the team, or build now.' : 'Build is available once there is a first draft.');
  if (result.ready) tutHook('ready');
}

/* ------------------------------------------------------------------ workspace open + team diff/animation */
async function openWorkspace(){
  if (W.phase !== 'meet') return;
  W.phase = 'design';
  $('#phase-label').textContent = 'design';
  const ws = $('#ws'); ws.classList.remove('meet'); ws.classList.add('open');
  await sleep(500);                                                  // grid transition
  layoutAll(false);
}

function orderIds(){
  const tops = [...W.agents.values()].filter(a => a.reports_to === 'atlas' || !W.agents.has(a.reports_to));
  const out = [];
  tops.forEach(t => { out.push(t.id); (t.members || []).forEach(m => { if (W.agents.has(m)) out.push(m); }); });
  W.agents.forEach(a => { if (!out.includes(a.id)) out.push(a.id); });
  return out;
}

async function applyBlueprint(bp){
  W.bp = bp;
  // positions the owner dragged to, saved with the blueprint: the canvas never snaps back after a reply (local wins)
  Object.entries(bp.layout || {}).forEach(([id, p]) => { if (!W.manual.has(id) && p && typeof p.x === 'number') W.manual.set(id, {x: p.x, y: p.y}); });
  const incoming = new Map();
  (bp.agents || []).forEach((a, i) => incoming.set(a.id, {id: a.id, name: a.name, role: a.role || '', goal: a.goal || '', instructions: a.instructions || [],
    tools: a.tools || [], engine: a.engine || 'atlas', reports_to: a.reports_to || 'atlas', members: a.members || [], color: a.color || PALETTE[i % PALETTE.length]}));
  const business = bp.business || {};
  if (business.name) { W.deskName = business.name; $('#bz-name').textContent = business.name; }
  const first = W.phase === 'meet';
  if (first) { await openWorkspace(); }
  const added = [], removed = [], moved = [], changed = [];
  incoming.forEach((a, id) => { const old = W.agents.get(id); if (!old) added.push(a); else { if (old.reports_to !== a.reports_to) moved.push(a); else if (old.role !== a.role || old.name !== a.name || (old.tools || []).join() !== (a.tools || []).join()) changed.push(a); } });
  W.agents.forEach((a, id) => { if (!incoming.has(id)) removed.push(a); });
  // narrate what Atlas is doing to the team
  if (first && added.length) addMsg('s', 'drafting team', 'assign');
  // update state
  W.agents = incoming;                                               // blueprint order, not first-seen order
  W.order = orderIds();
  // animate
  removed.forEach(a => { const el = nodeEl(a.id); if (el) { el.classList.add('gone'); setTimeout(() => el.remove(), 500); } removeEdge(a.id); addMsg('s', `− ${a.name} removed`, 'assign'); });
  layoutAll(true, added.map(a => a.id));
  for (const a of added) {
    const lead = a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? W.agents.get(a.reports_to) : null;
    addMsg('s', `+ ${a.name} — ${a.role}${lead ? `  (in ${lead.name}'s team)` : ''}`, 'assign');
    await spawn(a.id, lead ? lead.id : 'atlas');
    await sleep(160);
  }
  moved.forEach(a => { const to = a.reports_to === 'atlas' ? 'Atlas' : (W.agents.get(a.reports_to) || {}).name || a.reports_to; addMsg('s', `↳ ${a.name} now reports to ${to}`, 'assign'); removeEdge(a.id); drawEdge(a.id, true); });
  changed.forEach(a => { const el = nodeEl(a.id); if (el) { renderNodeInner(el, a); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); } });
  W.addedCams.forEach((src, name) => { const list = bp.cameras = bp.cameras || []; const c = list.find(x => x.name === name); if (c) { if (!c.source) c.source = src; } else list.push({name, source: src, journal: true, notes: '', focus: ''}); });   // hand-attached feeds survive a redraft
  await applyCams(bp.cameras || []);
  if (first) { tutHook('team'); }
  renderAll();
  if (W.sel && !W.agents.has(W.sel)) closeInsp();
  else if (W.sel) inspect(W.sel);
}

async function spawnAll(){
  layoutAll(false);
  for (const id of W.order) { const a = W.agents.get(id); const lead = a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? a.reports_to : 'atlas'; await spawn(id, lead); await sleep(140); }
}

/* ---- layout: Atlas col 0, top-level col 1, members col 2; vertical stack centred */
const NW = 236, NW_ATLAS = 214, COL = 300, ROW_MAX = 118, GAP_MAX = 26;
function layoutAll(animate, freshIds){
  const c = $('#canvas'); const H0 = c.clientHeight || 600, W0 = c.clientWidth || 900;
  const tops = W.order.filter(id => { const a = W.agents.get(id); return a && (a.reports_to === 'atlas' || !W.agents.has(a.reports_to)); });
  const blocks = tops.map(id => ({id, members: (W.agents.get(id).members || []).filter(m => W.agents.has(m))}));
  const rowsN = blocks.reduce((n, b) => n + Math.max(1, b.members.length), 0);
  const tight = rowsN * ROW_MAX + Math.max(0, blocks.length - 1) * GAP_MAX > H0 - 40;      // a tall team: close the gaps before shrinking
  const ROW = tight ? 100 : ROW_MAX, GAP = tight ? 6 : GAP_MAX;
  const heights = blocks.map(b => Math.max(1, b.members.length) * ROW);
  const total = heights.reduce((s, h) => s + h, 0) + Math.max(0, blocks.length - 1) * GAP;
  const hasMembers = blocks.some(b => b.members.length);
  const needW = NW_ATLAS + COL + (hasMembers ? COL : 0) + (NW - 30) + 48;
  const k = W.k = Math.max(.5, Math.min(1, (H0 - 40) / Math.max(1, total), W0 / needW)) * (W.uz || 1);
  const H = H0 / k, Wd = W0 / k;
  W.vb = [Wd, H];                                                    // the edges' viewBox must match the box the nodes were laid out in
  ['#nodes', '#edges'].forEach(q => { const st = $(q).style; st.inset = 'auto'; st.left = st.top = '0'; st.width = Wd + 'px'; st.height = H + 'px'; st.transformOrigin = '0 0'; }); applyView();
  const x0 = Math.max(24, Math.round((Wd - (NW_ATLAS + COL + (hasMembers ? COL : 0) + (NW - 30))) / 2));
  let y = Math.max(20, Math.round((H - total) / 2));
  const atlasY = Math.max(20, Math.round(H / 2 - 48));
  setPos('atlas', x0, atlasY, NW_ATLAS, animate);
  blocks.forEach((b, i) => {
    const blockH = heights[i]; const yT = y + Math.round(blockH / 2 - ROW / 2);
    setPos(b.id, x0 + COL, yT, NW, animate && !(freshIds || []).includes(b.id));
    b.members.forEach((m, j) => setPos(m, x0 + COL * 2, y + j * ROW, NW, animate && !(freshIds || []).includes(m)));
    y += blockH + GAP;
  });
  ensureAtlasNode();
  $('#empty').classList.toggle('hide', W.agents.size > 0);
  redrawEdges(); if (animate) animateEdges(900);
}
function setPos(id, x, y, w, animate){ const m = W.manual.get(id); if (m) { x = m.x; y = m.y; } W.pos.set(id, {x, y, w}); const el = nodeEl(id); if (el && !el.classList.contains('spawn')) { if (!animate) el.style.transition = 'none'; el.style.transform = `translate(${x}px,${y}px)`; if (!animate) { void el.offsetWidth; el.style.transition = ''; } } }
function nodeEl(id){ return document.getElementById('n-' + id.replace(/[^a-zA-Z0-9_#-]/g, '-')); }
function ensureAtlasNode(){
  let el = nodeEl('atlas'); const p = W.pos.get('atlas');
  if (!el) { el = document.createElement('div'); el.className = 'node atlas'; el.id = 'n-atlas'; el.dataset.id = 'atlas'; el.onclick = () => { if (!W.dragged) inspect('atlas'); };
    el.innerHTML = `<div class="nm">Atlas <span class="tag lead">lead</span></div><div class="role">Orchestrator — briefs, reviews, approves</div><div class="asg"></div><div class="chips"></div><div class="out"></div><div class="ft"></div>`;
    el.style.transition = 'none'; el.style.transform = `translate(${p.x}px,${p.y}px)`; $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = ''; }
  else el.style.transform = `translate(${p.x}px,${p.y}px)`;
}
function renderNodeInner(el, a){
  const isLead = (a.members || []).length > 0; const member = a.reports_to && a.reports_to !== 'atlas' && W.agents.has(a.reports_to);
  el.style.setProperty('--c', a.color);
  el.querySelector('.nm').innerHTML = `${esc(a.name)} <span class="tag ${isLead ? 'lead' : ''}">${isLead ? 'sub-team lead' : (member ? '↳ ' + esc((W.agents.get(a.reports_to) || {}).name || a.reports_to) : (a.engine === 'hermes_agent' ? 'hermes' : 'idle'))}</span>`;
  el.querySelector('.role').textContent = a.role;
  if (!el.querySelector('.chips').children.length || !W.run) el.querySelector('.chips').innerHTML = (a.tools || []).slice(0, 5).map(t => `<span>${esc(t)}</span>`).join('');
}
async function spawn(id, fromId){
  const a = W.agents.get(id); if (!a) return; if (nodeEl(id)) { renderNodeInner(nodeEl(id), a); return; }
  const from = W.pos.get(fromId) || W.pos.get('atlas') || {x: 40, y: 40, w: 200}; const to = W.pos.get(id) || from;
  const el = document.createElement('div'); el.className = 'node spawn'; el.id = 'n-' + id; el.dataset.id = id; el.onclick = () => { if (!W.dragged) inspect(id); };
  el.innerHTML = `<div class="nm"></div><div class="role"></div><div class="asg"></div><div class="chips"></div><div class="out"></div><div class="ft"></div>`;
  renderNodeInner(el, a);
  el.style.transition = 'none'; el.style.transform = `translate(${to.x}px,${to.y}px)`;      // fades in place, no fly-out
  $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = '';
  await sleep(20);
  el.classList.remove('spawn'); el.style.transform = `translate(${to.x}px,${to.y}px)`;
  const fromEl = nodeEl(fromId); if (fromEl) { fromEl.classList.remove('flash'); void fromEl.offsetWidth; fromEl.classList.add('flash'); }
  animateEdges(850); setTimeout(() => drawEdge(id, true), 120);
}

/* ---- edges (parent right-middle -> child left-middle) */
function edgeD(fromId, toId){
  const f = W.pos.get(fromId), t = W.pos.get(toId); if (!f || !t) return '';
  const fe = nodeEl(fromId), te = nodeEl(toId);
  const fh = fe ? fe.offsetHeight : 90, th = te ? te.offsetHeight : 90;
  const x1 = f.x + f.w, y1 = f.y + fh / 2, x2 = t.x, y2 = t.y + th / 2, mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}
function parentOf(id){ const a = W.agents.get(id); if (!a) return 'atlas'; return a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? a.reports_to : 'atlas'; }
function drawEdge(id, animate){
  const svg = $('#edges'); let p = svg.querySelector(`[data-e="${id}"]`);
  if (!p) { p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.dataset.e = id; p.setAttribute('pathLength', '1'); svg.appendChild(p); }
  p.setAttribute('d', edgeD(parentOf(id), id));
  if (animate) { p.classList.remove('draw'); void p.getBoundingClientRect(); p.classList.add('draw'); }
}
function removeEdge(id){ const p = $('#edges').querySelector(`[data-e="${id}"]`); if (p) p.remove(); }
function redrawEdges(){
  const svg = $('#edges'); const c = $('#canvas'); const k = W.k || 1; const vb = W.vb || [c.clientWidth / k, c.clientHeight / k]; svg.setAttribute('viewBox', `0 0 ${vb[0]} ${vb[1]}`);
  W.agents.forEach((a, id) => { if (nodeEl(id)) { const p = svg.querySelector(`[data-e="${id}"]`); if (p) p.setAttribute('d', edgeD(parentOf(id), id)); else drawEdge(id, false); } });
  if (W.run) W.run.inst.forEach((s, inst) => { if (s.ghost) { const p = svg.querySelector(`[data-e="${inst}"]`); if (p) p.setAttribute('d', edgeD(s.parent, inst)); } });
}
function animateEdges(ms){ const end = performance.now() + ms; cancelAnimationFrame(W.edgeAnim); const step = () => { redrawEdges(); if (performance.now() < end) W.edgeAnim = requestAnimationFrame(step); }; W.edgeAnim = requestAnimationFrame(step); }
// re-lay out whenever the canvas changes size: window resize, the column opening, the event log or camera strip appearing
if (window.ResizeObserver) new ResizeObserver(() => { if (W.phase === 'meet') return; cancelAnimationFrame(W.relayout); W.relayout = requestAnimationFrame(() => layoutAll(false)); }).observe(document.getElementById('canvas'));
else window.addEventListener('resize', () => { if (W.phase !== 'meet') layoutAll(false); });

/* ------------------------------------------------------------------ inspector */
function inspect(id){
  W.sel = id; document.querySelectorAll('.node.sel').forEach(n => n.classList.remove('sel')); const el = nodeEl(id); if (el) el.classList.add('sel');
  const a = id === 'atlas' ? {name: 'Atlas', role: 'Orchestrator', goal: 'Briefs the team, runs leads in parallel, reviews every result, and holds anything outbound for your approval.', instructions: [], tools: ['delegate', 'queue_action', 'crm_update', 'save_deliverable'], engine: 'atlas', reports_to: ''} : W.agents.get(id);
  if (!a) return;
  $('#insp-name').textContent = a.name;
  const st = W.run && [...W.run.inst.values()].filter(s => s.base === id);
  const out = st && st.length ? st.map(s => `<h4>${esc(s.inst)} · ${esc(s.status)}${s.assignment ? ' — ' + esc(s.assignment) : ''}</h4><pre id="live-${esc(s.inst)}">${esc(s.text || '(waiting…)')}</pre>`).join('') : '';
  const ask = id === 'atlas' ? [] : [`Give ${a.name} web access`, `Make ${a.name} lead a pod of 3`, `Merge ${a.name} into another role`, `Remove ${a.name}`, `Rewrite ${a.name}'s standing orders to be stricter`];
  $('#insp-body').innerHTML = `
    ${out}
    <h4>Role</h4><div>${esc(a.role)}</div>
    ${a.goal ? `<h4>Goal</h4><div>${esc(a.goal)}</div>` : ''}
    ${(a.instructions || []).length ? `<h4>Standing orders</h4><ul>${a.instructions.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    <h4>Tools</h4><div class="chips">${(a.tools || []).map(t => `<span>${esc(t)}</span>`).join('') || '<span class="hint">none</span>'}</div>
    <h4>Engine · reports to</h4><div>${esc(a.engine || 'atlas')} · ${esc(a.reports_to === 'atlas' || !a.reports_to ? 'Atlas' : ((W.agents.get(a.reports_to) || {}).name || a.reports_to))}${(a.members || []).length ? ` · leads ${a.members.map(m => esc((W.agents.get(m) || {}).name || m)).join(', ')}` : ''}</div>
    ${W.phase === 'design' && ask.length ? `<h4>Ask Atlas to…</h4><div class="ask">${ask.map(s => `<button onclick="closeInsp();send(${JSON.stringify(s).replace(/"/g, '&quot;')})">${esc(s)}</button>`).join('')}</div>` : ''}`;
  $('#insp').classList.add('on');
  document.querySelectorAll('.team tr.sel').forEach(r => r.classList.remove('sel')); const tr = document.getElementById('tr-' + id); if (tr) tr.classList.add('sel');
}
function closeInsp(){ W.sel = null; $('#insp').classList.remove('on'); document.querySelectorAll('.node.sel, .team tr.sel').forEach(n => n.classList.remove('sel')); }

/* ------------------------------------------------------------------ build */
async function buildDesk(){
  if (!W.bp || !(W.bp.agents || []).length) return;
  $('#build-btn').disabled = true; $('#build-hint').textContent = 'Building…'; setState('building');
  const r = await api(`/design/${W.sid}/build`, {method: 'POST', body: {blueprint: W.bp, tier: W.tier, name: W.deskName || ''}});
  setState('ready');
  if (!r || r.error) { $('#build-hint').textContent = (r && r.error) || 'build failed'; $('#build-btn').disabled = false; return; }
  W.deskId = r.desk.id; W.deskName = r.desk.business_name || r.desk.name; $('#bz-name').textContent = W.deskName; setDeskStatus(); saveView();
  (r.cameras || []).forEach(c => { const k = W.cams.get(c.name); if (k) { k.id = c.id; k.journal = c.journal; setCamState(k, 'starting…', ''); } });
  const camLine = (r.cameras || []).length ? ` ${r.cameras.length} camera${r.cameras.length === 1 ? '' : 's'} writing the journal; ask about what they see.` : '';
  const missing = (r.cameras_missing || []).length ? ` Still needed: the stream address for ${r.cameras_missing.join(', ')} (add it on the Cameras page of the full dashboard).` : '';
  addMsg('a', `Desk built: ${W.agents.size} agent${W.agents.size === 1 ? '' : 's'}. Outbound messages wait for approval.${camLine}${missing} Give the team a job in the bar above.`);
  toast('Desk built');
  enterRunMode();
  if ((r.cameras || []).length) { startCamPoll(); setSugg(CAM_QUESTIONS); tutHook('watching'); } else tutHook('built');
}
function enterRunMode(){
  W.phase = 'run'; $('#ws').classList.add('run');
  $('#phase-label').textContent = 'ready';
  $('#chat-foot').classList.add('hide'); $('#build-btn').classList.add('hide'); $('#newjob-btn').classList.remove('hide');
  $('#say').placeholder = W.sid ? 'Describe a change to the team, or type a job' : (W.cams.size ? 'Ask about the cameras, or type a job' : 'Type a job for the team');
  document.querySelectorAll('.node .nm .tag').forEach(t => { if (!t.classList.contains('lead') && !t.textContent.startsWith('↳')) t.textContent = 'idle'; });
  loadApprovals(); loadRuns(); pollHealth(); renderAll();
}

/* ------------------------------------------------------------------ run: real events, live output */
async function deploy(){
  const task = $('#job').value.trim(); if (!task) { $('#job').focus(); return toast('Type the job first'); }
  if (W.run && W.run.active) return toast('A run is already going — wait for it to finish');
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});   // this tab's desk, even with other tabs open
  const r = await api('/runs', {method: 'POST', body: {task, mode: 'auto'}});
  if (!r || r.error) return toast((r && (r.message || r.error)) || 'could not start');
  $('#job').value = '';
  addMsg('u', task); addMsg('s', `run ${r.run_id} started`, 'assign');
  $('#summary').classList.add('hide'); $('#feed').innerHTML = '';
  document.querySelectorAll('.node.ghost').forEach(n => n.remove()); $('#edges').querySelectorAll('[data-e*="#"]').forEach(p => p.remove());
  document.querySelectorAll('.node').forEach(n => { n.classList.remove('busy', 'done', 'error'); n.querySelector('.out').textContent = ''; n.querySelector('.asg').textContent = ''; n.querySelector('.ft').textContent = ''; const t = n.querySelector('.nm .tag'); if (t && !t.classList.contains('lead') && !t.textContent.startsWith('↳')) { t.textContent = 'idle'; t.className = 'tag'; } });
  W.agents.forEach(a => { const el = nodeEl(a.id); if (el) el.querySelector('.chips').innerHTML = ''; });
  const atlas = nodeEl('atlas'); if (atlas) atlas.querySelector('.chips').innerHTML = '';
  W.run = {id: r.run_id, inst: new Map(), active: true, tin: 0, tout: 0, task, started: Date.now(), status: ''};
  $('#phase-label').textContent = 'running'; setState('running');
  $('#sb-run').classList.remove('hide'); $('#sb-run-id').textContent = r.run_id; $('#run-cancel').classList.remove('hide'); $('#tab-n-runs').classList.remove('hide');
  $('#summary-empty').classList.remove('hide');
  renderRunPanel(); renderAll(); showTab('overview'); if (isPhone()) mtab('overview');
  const es = new EventSource(`/api/stream?run=${encodeURIComponent(r.run_id)}&since=0`);
  W.run.es = es;
  es.onmessage = m => { try { onRunEvent(JSON.parse(m.data)); } catch (_) {} };
  es.onerror = () => { if (W.run && !W.run.active) es.close(); };
  tutHook('running');
}
function instState(e){
  const inst = (e.data && e.data.inst) || e.agent; const base = inst.split('#')[0];
  let s = W.run.inst.get(inst);
  if (!s) { s = {inst, base, text: '', status: 'idle', chips: [], parent: (e.data && e.data.parent) || 'atlas', ghost: inst !== base, turns: 0}; W.run.inst.set(inst, s); if (s.ghost) makeGhost(s); }
  return s;
}
function makeGhost(s){
  const baseEl = nodeEl(s.base); const bp = W.pos.get(s.base); if (!baseEl || !bp) return;
  const n = parseInt(s.inst.split('#')[1] || '2', 10); const x = bp.x + 22 * (n - 1), y = bp.y + 34 * (n - 1);
  W.pos.set(s.inst, {x, y, w: bp.w});
  const el = baseEl.cloneNode(true); el.id = 'n-' + s.inst.replace(/[^a-zA-Z0-9_#-]/g, '-'); el.classList.add('ghost', 'spawn'); el.classList.remove('sel', 'busy', 'done');
  el.onclick = () => inspect(s.base); el.querySelector('.nm').innerHTML = `${esc((W.agents.get(s.base) || {}).name || s.base)} <span class="tag">#${n}</span>`; el.querySelector('.out').textContent = ''; el.querySelector('.chips').innerHTML = ''; el.querySelector('.asg').textContent = '';
  el.style.transition = 'none'; el.style.transform = `translate(${bp.x}px,${bp.y}px) scale(.6)`; $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = '';
  requestAnimationFrame(() => { el.classList.remove('spawn'); el.style.transform = `translate(${x}px,${y}px)`; });
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.dataset.e = s.inst; p.setAttribute('pathLength', '1'); p.setAttribute('d', edgeD(s.parent, s.inst)); p.classList.add('draw'); $('#edges').appendChild(p);
  animateEdges(700);
}
function elFor(s){ return s.ghost ? nodeEl(s.inst) : nodeEl(s.base); }
function setTag(el, text, cls){ const t = el && el.querySelector('.nm .tag'); if (!t || t.classList.contains('lead') || t.textContent.startsWith('↳')) return; t.textContent = text; t.className = 'tag ' + (cls || ''); }
function feed(e, cls){ const f = $('#feed'); const d = document.createElement('div'); d.className = cls || e.kind; const t = new Date((e.ts || Date.now() / 1000) * 1000).toISOString().slice(11, 19); d.innerHTML = `<span class="t">${t}</span><span class="a">${esc((e.data && e.data.inst) || e.agent)}</span><span class="k">${esc(e.kind)}</span><span class="x">${esc(e.text || '')}</span>`; f.appendChild(d); f.scrollTop = 1e9; while (f.children.length > 120) f.firstChild.remove(); }
function onRunEvent(e){
  if (!W.run || e.run_id !== W.run.id) return;
  if (typeof e.data === 'string') { try { e.data = JSON.parse(e.data || '{}'); } catch (_) { e.data = {}; } }
  e.data = e.data || {};
  if (e.kind === 'usage') { W.run.tin = e.data.tokens_in; W.run.tout = e.data.tokens_out; $('#phase-label').textContent = `running · ${(W.run.tin || 0).toLocaleString()} tok in`; renderRunMeta(); return; }
  if (e.kind === 'token') { if (e.data.thinking) return; const s = instState(e); s.text += e.text; if (s.text.length > 6000) s.text = s.text.slice(-6000); const el = elFor(s); if (el) { const o = el.querySelector('.out'); o.textContent = s.text.slice(-420); } const live = document.getElementById('live-' + s.inst); if (live) { live.textContent = s.text; live.scrollTop = 1e9; } if (s.status !== 'writing') { s.status = 'writing'; setTag(el, 'writing', 'busy'); renderRunPanel(); } else renderRunLive(s); return; }
  if (e.agent !== 'system' && e.agent !== 'owner') {
    const s = instState(e); const el = elFor(s);
    if (e.kind === 'agent_start') { s.status = 'thinking'; s.assignment = e.data.assignment || ''; s.text = ''; if (el) { el.classList.add('busy'); el.classList.remove('done', 'error'); el.querySelector('.asg').textContent = s.assignment; el.querySelector('.out').textContent = ''; setTag(el, 'thinking', 'busy'); } edgeState(s.inst === 'atlas' ? null : (s.ghost ? s.inst : s.base), 'on'); if (s.base !== 'atlas') addMsg('s', `→ ${(W.agents.get(s.base) || {}).name || s.base}: ${s.assignment.slice(0, 90)}`, 'assign'); }
    else if (e.kind === 'log') { s.turns++; s.status = 'thinking'; if (el) { setTag(el, `turn ${s.turns}`, 'busy'); el.querySelector('.ft').textContent = `${s.turns} turn${s.turns === 1 ? '' : 's'}`; } }
    else if (e.kind === 'tool') { const name = e.text.split(/[( →:]/)[0]; s.chips.push(name); if (el) { const c = el.querySelector('.chips'); c.querySelectorAll('.hot').forEach(x => x.classList.remove('hot')); const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = name; c.appendChild(sp); while (c.children.length > 7) c.firstChild.remove(); setTag(el, name, 'busy'); } if (name === 'delegate') { const m = e.text.match(/delegate → ([\w#-]+)/); if (m) edgeState(m[1], 'on'); } }
    else if (e.kind === 'agent_end') { s.status = 'done'; if (el) { el.classList.remove('busy'); el.classList.add('done'); setTag(el, 'done ✓', 'done'); } edgeState(s.ghost ? s.inst : s.base, 'done'); }
    else if (e.kind === 'error') { s.status = 'error'; if (el) { el.classList.remove('busy'); el.classList.add('error'); setTag(el, 'error', ''); } }
    else if (e.kind === 'approval') { if (el) { const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = '⏸ ' + (e.data.action_kind || 'approval'); el.querySelector('.chips').appendChild(sp); } loadApprovals(); toast('Waiting for your approval: ' + (e.data.action_kind || 'an outbound action')); tutHook('approval'); }
    else if (e.kind === 'policy') { if (el) { const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = 'policy ✋'; el.querySelector('.chips').appendChild(sp); } }
    if (W.sel === s.base && ['agent_start', 'agent_end', 'tool', 'log'].includes(e.kind)) inspect(s.base);
    if (['agent_start', 'agent_end', 'tool', 'log', 'error', 'approval'].includes(e.kind)) { renderRunPanel(); renderTeamTable(); renderExplorer(); }
  }
  if (e.kind === 'done') {
    W.run.active = false; W.run.ended = Date.now(); W.run.status = e.data.status || 'done'; if (W.run.es) W.run.es.close();
    $('#run-cancel').classList.add('hide'); $('#tab-n-runs').classList.add('hide'); $('#summary-empty').classList.add('hide');
    renderRunPanel(); renderAll(); loadRuns();
    document.querySelectorAll('.node.busy').forEach(n => { n.classList.remove('busy'); n.classList.add('done'); setTag(n, 'done ✓', 'done'); });
    $('#edges').querySelectorAll('path.on').forEach(p => { p.classList.remove('on'); p.classList.add('done'); });
    $('#phase-label').textContent = (e.data.status || 'done'); setState('ready');
    $('#summary-text').innerHTML = md((e.text || '').trim()); $('#summary').classList.remove('hide');
    addMsg('a', `Run finished. ${(e.text || '').split('---').pop().trim()}`.trim());
    loadApprovals(); tutHook('done');
  }
  if (!['token', 'usage', 'log'].includes(e.kind)) feed(e);
}
function edgeState(id, state){ if (!id) return; const p = $('#edges').querySelector(`[data-e="${id}"]`); if (!p) return; p.classList.remove('draw', 'on', 'done'); p.classList.add(state); }

/* ------------------------------------------------------------------ approvals */
async function loadApprovals(){
  if (!W.deskId && W.phase !== 'run') return;
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});
  const list = await api('/actions?status=pending') || [];
  const n = Array.isArray(list) ? list.length : 0;
  W.apCount = n; W.apOldest = n ? ago(Math.min(...list.map(a => a.created || Date.now() / 1000))) : '';
  ['#ap-n', '#ap-n2', '#ap-n3'].forEach(q => { const el = $(q); if (el) { el.textContent = n; el.classList.toggle('hide', !n); } });
  $('#tab-n-ap').textContent = n; $('#tab-n-ap').className = 'n' + (n ? ' warn' : ''); $('#ap-oldest').textContent = n ? 'oldest ' + W.apOldest : '';
  const hq = $('#h-queue'); if (hq) hq.innerHTML = n ? `<i class="dot d-warn"></i>${n} approval${n === 1 ? '' : 's'}` : '<i class="dot d-ok"></i>empty';
  $('#ap-body').innerHTML = n ? list.map(a => `<div class="ap" id="ap-${a.id}">
      <div class="l1"><svg class="i i14"><use href="#${a.kind === 'email' ? 'i-mail' : 'i-chat'}"/></svg><b>${esc(a.to || '(no recipient)')}</b><span class="age">${ago(a.created)}</span></div>
      <div class="l2">${esc(a.kind)} · ${a.subject ? '"' + esc(a.subject) + '"' : esc((a.body || '').slice(0, 90))} · by ${esc(a.agent || a.by || 'atlas')}</div>
      ${a.flags ? `<div class="flag"><svg class="i"><use href="#i-alert"/></svg>${esc(a.flags)}</div>` : ''}
      ${a.reason ? `<div class="hint" style="margin-top:4px">${esc(a.reason)}</div>` : ''}
      <div class="edit"><input id="ap-s-${a.id}" value="${esc(a.subject || '')}" placeholder="Subject"><textarea id="ap-b-${a.id}">${esc(a.body || '')}</textarea></div>
      <div class="acts"><button class="btn btn-s btn-ok" onclick="decide(${a.id},'approved')"><svg class="i"><use href="#i-check"/></svg>Approve &amp; send</button><button class="btn btn-s" onclick="apEdit(${a.id})"><svg class="i"><use href="#i-edit"/></svg>Edit</button><button class="btn btn-s btn-d" onclick="decide(${a.id},'rejected')"><svg class="i"><use href="#i-x"/></svg>Reject</button></div>
      <input class="note" id="ap-n-${a.id}" placeholder="Note for the agents (optional)"></div>`).join('')
    : '<div class="empty sm"><span>Nothing waiting. When an agent wants to send something, it lands here first.</span></div>';
  renderKpis(); renderExplorer();
}
function apEdit(id){ const el = document.getElementById('ap-' + id); if (!el) return; el.classList.toggle('editing'); if (el.classList.contains('editing')) $(`#ap-b-${id}`).focus(); }
function ago(ts){ if (!ts) return ''; const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? 'now' : s < 3600 ? Math.round(s / 60) + ' min' : s < 86400 ? Math.round(s / 3600) + ' h' : Math.round(s / 86400) + ' d'; }
async function decide(id, status){
  const r = await api(`/actions/${id}/decide`, {method: 'POST', body: {status, note: $(`#ap-n-${id}`).value, body: $(`#ap-b-${id}`).value, subject: $(`#ap-s-${id}`).value}});
  if (!r || r.error) return toast((r && r.error) || 'failed');
  toast(status === 'approved' ? (W.mode === 'demo' ? 'Approved (simulated send in demo mode)' : 'Approved and sent') : 'Rejected — the agents will see your note next run');
  loadApprovals();
}
function toggleApprovals(){ loadApprovals(); showTab('approvals'); if (isPhone()) mtab('approvals'); }

/* ------------------------------------------------------------------ tutorial (coach marks driven by what actually happens) */
const TUT = {steps: [], i: -1, auto: true, on: false};
const TUT_STEPS = {
  meet: [
    {t: '#chat-head', h: 'Atlas', p: 'Atlas designs the team, briefs every agent, checks their work and never sends anything without your approval.'},
    {t: '#composer', h: 'Tell it about your business', p: 'One or two sentences: what you do, and the job that eats your time. Try a suggestion chip if you want a quick start. Atlas will open the workspace and assemble the team as you talk.', end: true},
  ],
  team: [
    {t: '#team-panel', h: 'Atlas assigned your team', p: 'Each row is an agent with its own role, standing orders and tools. Sub-team leads run their own members. Click a row to read its brief; Org chart shows the structure.'},
    {t: '#composer', h: 'Reshape it by talking', p: 'Say things like "add a QA reviewer", "split research into a pod of 3", "the writer should report to the researcher". The structure morphs live.'},
    {t: '#build-btn', h: 'Build when it looks right', p: 'Building creates the desk with this team. You can still change everything later.', end: true},
  ],
  built: [
    {t: '#newjob-btn', h: 'Give the team a job', p: 'New job (N) opens Runs. Paste a customer enquiry, ask for a report, a comparison, a chase list — anything. Atlas briefs the team and you watch them work in real time.', end: true},
  ],
  running: [
    {t: '#run-panel', h: 'Watch them work', p: 'Each step is an agent: it lights up as it starts, its output streams here, tool calls and hand-backs land in Activity. Click a row in Team to read the full live output.', end: true},
  ],
  cameras: [
    {t: '#camstrip', h: 'Cameras join the desk', p: 'Each tile is a camera. With the journal on, it writes a detailed note whenever something changes, and a summary every 15 minutes. After you build, the live picture and the latest note show here.', end: true},
  ],
  watching: [
    {t: '#camstrip', h: 'The cameras are documenting', p: 'Every few seconds each camera looks, compares with its last note and writes down what changed: who arrived or left, what they wear and carry, how long they waited.'},
    {t: '#composer', h: 'Ask anything', p: 'Type a question like "Was any bag left unattended?" or "How long did the guest at reception wait?". Atlas answers from the journal with times, and shows the frames it used.'},
    {t: '#journal-link', h: 'Read the whole day', p: 'The journal is also a readable page, one per day, filterable by camera.'},
    {t: '#jobbar', h: 'Give the team a job', p: 'Ask for a report or a summary, paste an enquiry, anything. You watch every agent work, and nothing leaves without your approval.', end: true},
  ],
  approval: [
    {t: '#ap-panel', h: 'Nothing goes out without you', p: 'An agent wants to send something. It waits under Approvals: edit the text if you like, then approve or reject with a note — the note tunes the agents next time.', end: true},
  ],
  done: [
    {t: '#run-panel', h: 'The run is done', p: 'The result under Runs is verified by the desk (what was queued, sent, saved), not claimed by the model. Give the team another job, or tell Atlas what to change in the team.', end: true},
  ],
};
function tutStart(force, phase){
  if ((localStorage.getItem('ws_tut_done') || isPhone()) && !force) { TUT.auto = false; return; }   // phones: the guide is behind the help button
  TUT.auto = true; const key = phase || (W.phase === 'run' ? 'built' : (W.agents.size ? 'team' : 'meet'));
  tutShow(TUT_STEPS[key] || TUT_STEPS.meet);
}
function tutHook(ev){ if (!TUT.auto || TUT.on) return; const steps = TUT_STEPS[ev]; if (steps) setTimeout(() => tutShow(steps), ev === 'team' ? 900 : 500); }
function tutShow(steps){ TUT.steps = steps; TUT.i = -1; TUT.on = true; $('#coach').classList.remove('hide'); tutNext(); }
function tutNext(){
  document.querySelectorAll('.coach-target').forEach(e => e.classList.remove('coach-target'));
  TUT.i++; const s = TUT.steps[TUT.i];
  if (!s) { tutEnd(); return; }
  const target = $(s.t); const box = $('#coach-box');
  $('#coach-step').textContent = `${TUT.i + 1} / ${TUT.steps.length}`; $('#coach-title').textContent = s.h; $('#coach-text').textContent = s.p;
  $('#coach-next').textContent = TUT.i === TUT.steps.length - 1 ? (s.end ? 'Got it' : 'Done') : 'Next';
  box.className = '';
  if (target) { target.classList.add('coach-target'); const r = target.getBoundingClientRect(); const below = r.bottom + 220 < innerHeight; box.style.left = Math.max(12, Math.min(innerWidth - 360, r.left)) + 'px'; box.style.top = (below ? r.bottom + 14 : r.top - 14) + 'px'; if (!below) { box.classList.add('below'); box.style.transform = 'translateY(-100%)'; } else box.style.transform = ''; }
  else { box.classList.add('noarrow'); box.style.left = '50%'; box.style.top = '40%'; box.style.transform = 'translate(-50%,-50%)'; }
}
function tutEnd(){ TUT.on = false; $('#coach').classList.add('hide'); document.querySelectorAll('.coach-target').forEach(e => e.classList.remove('coach-target')); if (TUT.steps === TUT_STEPS.done || TUT.steps === TUT_STEPS.approval) localStorage.setItem('ws_tut_done', '1'); }
function tutSkip(){ TUT.auto = false; localStorage.setItem('ws_tut_done', '1'); tutEnd(); }


/* ------------------------------------------------------------------ cameras: tiles, live picture, journal, questions */
const CAM_QUESTIONS = ['What happened in the last 10 minutes?', 'Was anything left behind?', 'Who waited the longest?', 'Describe everyone who came in'];
function camKind(src){ src = String(src || ''); if (!src) return 'needs a stream address'; if (src.startsWith('sample:') || /AtlasDemo[\\/]videos|[\\/]samples[\\/]/i.test(src)) return 'sample footage'; if (/^rtsps?:/i.test(src)) return 'RTSP camera'; if (/^https?:/i.test(src)) return 'snapshot URL'; if (/^\d+$/.test(src)) return 'webcam'; if (/\.(mp4|mov|avi|mkv|webm|m4v|ts|mpe?g)$/i.test(src)) return 'recording'; if (/\.(jpe?g|png)$/i.test(src)) return 'still image'; return 'camera'; }
function setCamState(k, text, cls){ if (!k.el) return; const t = k.el.querySelector('.cn .tag'); t.textContent = text; t.className = 'tag ' + (cls || ''); }
async function applyCams(list, instant){
  const incoming = new Map((list || []).map(c => [c.name, c]));
  const first = !W.cams.size && incoming.size;
  W.cams.forEach((k, name) => { if (!incoming.has(name)) { if (k.el) { k.el.classList.add('spawn'); setTimeout(() => k.el.remove(), 500); } W.cams.delete(name); addMsg('s', `− camera ${name} removed`, 'assign'); } });
  $('#camstrip').classList.toggle('hide', !incoming.size);
  $('#ws').classList.toggle('hascams', incoming.size > 0);
  if (first) { layoutAll(true); animateEdges(700); }
  for (const [name, c] of incoming) {
    const k = W.cams.get(name);
    if (k) { Object.assign(k, {source: c.source, journal: c.journal !== false, alerts: !!c.alerts, id: c.id || k.id}); const kd = k.el.querySelector('.kind'); if (kd) kd.textContent = camKind(c.source); continue; }
    const nk = {name, id: c.id || null, source: c.source || '', journal: c.journal !== false, alerts: !!c.alerts, el: null, seenTs: 0};
    const el = document.createElement('div'); el.className = 'cam' + (instant ? '' : ' spawn');
    el.innerHTML = `<div class="pic"><span class="kind">${esc(camKind(c.source))}</span><span class="cnt"></span></div><div class="cb"><div class="cn">${esc(name)}<span class="tag ${nk.journal ? 'on' : ''}">${nk.journal ? 'journal on' : 'rules only'}</span></div><div class="note">${esc(c.notes || c.focus || 'waiting for the first note')}</div></div>`;
    el.onclick = () => openAttach(name);
    $('#cams').appendChild(el); nk.el = el; W.cams.set(name, nk);
    if (!instant) { addMsg('s', `+ camera ${name}: ${camKind(c.source)}${nk.journal ? ', keeping a journal' : ''}`, 'assign'); void el.offsetWidth; await sleep(30); el.classList.remove('spawn'); await sleep(170); }
  }
  $('#cam-none').classList.toggle('hide', incoming.size > 0);
  renderAll();
  if (first && !instant) tutHook('cameras');
}
function startCamPoll(){ clearInterval(W.camPoll); W.evSince = Date.now() / 1000 - 5; pollCams(); W.camPoll = setInterval(pollCams, 4000); }
async function pollCams(){
  if (!W.cams.size) return;
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});
  const r = await api('/cameras'); if (!r || !r.cameras) return;
  for (const c of r.cameras) {
    const k = W.cams.get(c.name); if (!k) continue; k.id = c.id;
    const s = c.seen || {}; const le = c.last_event || {};
    const ts = s.ts || le.ts || 0;
    if (ts && ts !== k.seenTs) {
      k.seenTs = ts;
      const pic = k.el.querySelector('.pic'); let img = pic.querySelector('img');
      const src = `/api/cameras/${c.id}/frame.jpg?t=${Math.round(ts * 1000)}`;
      const pre = new Image(); pre.onload = () => { if (!img) { img = document.createElement('img'); pic.appendChild(img); if (!pic.querySelector('.live')) { const lv = document.createElement('span'); lv.className = 'live'; lv.textContent = 'live'; pic.appendChild(lv); } } img.src = src; }; pre.src = src;
      const counts = Object.entries(s.counts || le.counts || {}).map(([k2, v]) => `${v} ${k2}`).join(', ');
      setCamState(k, s.journal ? 'writing note' : (k.journal ? 'journal on' : 'watching'), s.journal ? 'on' : (k.journal ? 'on' : ''));
      const cnt = k.el.querySelector('.cnt'); if (cnt) cnt.textContent = counts;
    }
    const note = s.journal || (le.source === 'journal' || le.source === 'digest' ? le.answer : '');
    if (note && note !== k.note) { k.note = note; k.noteTs = le.ts || s.ts || 0; const n = k.el.querySelector('.note'); n.innerHTML = (k.noteTs ? `<b>${new Date(k.noteTs * 1000).toISOString().slice(11, 16)}</b>` : '') + esc(note); n.classList.remove('new'); void n.offsetWidth; n.classList.add('new'); }
  }
  if (r.stats) { W.journalCount = r.stats.events; }
  renderKpis();
  const ev = await api(`/vision/events?hours=1&limit=20`) || [];
  if (Array.isArray(ev)) ev.filter(e => e.ts > W.evSince && (e.source === 'journal' || e.source === 'digest')).sort((a, b) => a.ts - b.ts).forEach(e => {
    W.evSince = Math.max(W.evSince, e.ts); W.journalLast = new Date(e.ts * 1000).toISOString().slice(11, 19);
    feed({ts: e.ts, agent: e.camera, kind: e.source === 'digest' ? 'summary' : 'journal', text: e.answer || e.reason || '', data: {}}, e.source);
    const k = W.cams.get(e.camera); if (k && k.el) { k.el.classList.remove('flash'); void k.el.offsetWidth; k.el.classList.add('flash'); setTimeout(() => k.el.classList.remove('flash'), 900); }
  });
}
function looksLikeQuestion(t){ return /\?\s*$/.test(t) || /^(who|what|when|where|why|how|was|were|did|does|do|is|are|has|have|had|any|anyone|anything|show|describe|tell me|list|summar|count)\b/i.test(t.trim()); }
async function askCams(text){
  addMsg('u', text); setSugg([]);
  W.busy = true; $('#send').disabled = true; setState('reading journal');
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});
  const r = await api('/vision/ask', {method: 'POST', body: {question: text, hours: 24}});
  W.busy = false; $('#send').disabled = false; setState('ready');
  if (!r || r.error) { addMsg('s', (r && r.error) || 'could not read the journal; try again'); return; }
  const d = await typeMsg(r.answer || 'Nothing in the journal answers that yet.');
  d.classList.add('cams');
  const ev = (r.evidence || []).filter(e => e.snapshot_url).slice(-6);
  if (ev.length) {
    const row = document.createElement('div'); row.className = 'evid';
    row.innerHTML = ev.map(e => `<a href="${esc(e.snapshot_url)}" target="_blank" rel="noopener" title="${esc(e.camera)} #${e.id}"><img src="${esc(e.snapshot_url)}" loading="lazy" alt=""><span>#${e.id} ${esc(e.camera)}</span></a>`).join('');
    d.appendChild(row); $('#msgs').scrollTop = 1e9;
    [...new Set(ev.map(e => e.camera))].forEach(n => { const k = W.cams.get(n); if (k && k.el) { k.el.classList.add('flash'); setTimeout(() => k.el.classList.remove('flash'), 1400); } });
  }
  const m = r.retrieval || {}; if (m.considered) addMsg('s', `searched ${m.considered} journal entries (${m.window || 'last 24h'}), ${m.grounding || ''}`);
  setSugg(CAM_QUESTIONS);
}

/* ------------------------------------------------------------------ front door: my desks, guide */
async function loadMyDesks(){
  const r = await api('/desks'); const list = (r && r.desks || []).filter(d => d.name && d.name !== 'My business').slice(0, 12);
  W.desks = list; renderExplorer();
  if (!list.length) return;
  const box = $('#mydesks');
  const day = ts => ts ? new Date(ts * 1000).toLocaleDateString('en-GB', {day: '2-digit', month: 'short'}) : '';
  box.innerHTML = '<div class="lh">Or open one of your desks</div>' + list.map(d => `<button onclick="openDesk(${d.id})"><span>${esc(d.business_name || d.name)}</span><span class="c">${esc((d.template || '').replace(/_/g, ' '))}</span><span class="c">${esc(d.tier || '')}</span><span class="c">#${d.id} · ${esc(day(d.created))}</span><svg class="i i14 go"><use href="#i-chev"/></svg></button>`).join('');
  box.classList.remove('hide');
}
async function openDesk(id){ await api(`/desks/${id}/select`, {method: 'POST', body: {}}); location.href = '/desk/workspace?desk=' + id; }
function openGuide(){ $('#guide').classList.remove('hide'); }
function closeGuide(){ $('#guide').classList.add('hide'); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { if (!$('#guide').classList.contains('hide')) closeGuide(); else if (!$('#palette').classList.contains('hide')) closePalette(); else if ($('#insp').classList.contains('on')) closeInsp(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '') || (e.target && e.target.isContentEditable);
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); $('#say').focus(); if (isPhone()) mtab('atlas'); }
  else if ((e.key === 'n' || e.key === 'N') && W.phase === 'run') { e.preventDefault(); newJob(); }
});


/* ------------------------------------------------------------------ free / paid models (always the owner's click) */
async function setTier(tier){
  if (!W.sid) return toast(W.deskId ? 'This desk was built on ' + (W.tier === 'free' ? 'free' : 'paid') + ' models; change it in Desk setup' : 'Start a conversation first');
  const r = await api(`/design/${W.sid}/tier`, {method: 'POST', body: {tier}});
  if (!r || r.error) return toast((r && r.error) || 'could not switch');
  W.tier = r.tier; setDeskStatus(); renderKpis();
  addMsg('s', W.tier === 'free' ? 'switched to free models' : 'switched to paid models: Claude for design, a paid vision model for the cameras', 'assign');
}
function toggleTier(){ return setTier(W.tier === 'free' ? 'balanced' : 'free'); }


/* ------------------------------------------------------------------ live view of one camera (motion JPEG with detections) */
function openLive(name){
  const k = W.cams.get(name); if (!k) return;
  if (!k.id) return toast('This camera goes live once the desk is built');
  $('#live-name').textContent = name;
  $('#live-note').textContent = k.note || 'The first journal note appears here within a few seconds.';
  $('#live-journal').href = `/api/vision/journal?format=html&camera=${encodeURIComponent(name)}`;
  $('#live-img').src = `/api/cameras/${k.id}/live.mjpg?fps=12&t=${Date.now()}`;
  $('#liveview').classList.remove('hide'); W.liveCam = name;
}
function closeLive(){ $('#live-img').src = ''; $('#liveview').classList.add('hide'); W.liveCam = null; }
document.addEventListener('keydown', e => { if (e.key === 'Escape' && W.liveCam) closeLive(); });
setInterval(() => { if (W.liveCam) { const k = W.cams.get(W.liveCam); if (k && k.note) $('#live-note').textContent = k.note; } }, 2000);


/* ------------------------------------------------------------------ formatted replies (a small, safe markdown subset) */
function mdInline(t){
  return esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(src){
  const lines = String(src ?? '').replace(/\r/g, '').split('\n'); const out = []; let list = null, para = [], code = null, table = null;
  const flushPara = () => { if (para.length) { out.push('<p>' + para.map(mdInline).join('<br>') + '</p>'); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>` + list.items.map(i => `<li>${mdInline(i)}</li>`).join('') + `</${list.tag}>`); list = null; } };
  const flushTable = () => { if (table) { const [head, ...rows] = table.filter(r => !/^\s*\|?\s*:?-{2,}/.test(r.join('|'))); out.push('<div class="tw"><table><thead><tr>' + head.map(c => `<th>${mdInline(c)}</th>`).join('') + '</tr></thead><tbody>' + rows.map(r => '<tr>' + r.map(c => `<td>${mdInline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>'); table = null; } };
  const flush = () => { flushPara(); flushList(); flushTable(); };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (code !== null) { if (/^```/.test(line)) { out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); code = null; } else code.push(raw); continue; }
    if (/^```/.test(line)) { flush(); code = []; continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); (table = table || []).push(line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim())); continue; }
    flushTable();
    let m;
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flush(); const lv = Math.min(4, m[1].length + 1); out.push(`<h${lv}>${mdInline(m[2])}</h${lv}>`); continue; }
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); out.push('<hr>'); continue; }
    if ((m = line.match(/^\s*>\s?(.*)$/))) { flush(); out.push(`<blockquote>${mdInline(m[1])}</blockquote>`); continue; }
    if ((m = line.match(/^\s*(?:[-*•])\s+(.*)$/))) { flushPara(); if (!list || list.tag !== 'ul') { flushList(); list = {tag: 'ul', items: []}; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); if (!list || list.tag !== 'ol') { flushList(); list = {tag: 'ol', items: []}; } list.items.push(m[1]); continue; }
    if (list && /^\s{2,}\S/.test(raw)) { list.items[list.items.length - 1] += ' ' + line.trim(); continue; }
    flushList(); para.push(line.trim());
  }
  if (code !== null) out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
  flush();
  return out.join('');
}

/* ------------------------------------------------------------------ canvas: drag cards, pan, zoom, remembered per desk */
function viewKey(){ return W.deskId ? 'ws_view_d' + W.deskId : (W.sid ? 'ws_view_s' + W.sid : ''); }
function loadView(){ try { const v = JSON.parse(localStorage.getItem(viewKey()) || 'null'); if (!v) return; W.manual = new Map(Object.entries(v.manual || {})); W.pan = v.pan || {x: 0, y: 0}; W.uz = v.uz || 1; } catch (_) {} }
function saveView(){ const k = viewKey(); if (!k) return; try { localStorage.setItem(k, JSON.stringify({manual: Object.fromEntries(W.manual), pan: W.pan, uz: W.uz})); } catch (_) {} pushLayout(); }
function pushLayout(){                                            // the drafted team's positions also live in the design session (survives another browser / a redraft)
  if (!W.sid || W.deskId) return; clearTimeout(W.layoutT);
  W.layoutT = setTimeout(() => { api(`/design/${W.sid}/layout`, {method: 'POST', body: {positions: Object.fromEntries(W.manual)}}).catch(() => {}); }, 400);
}
function applyView(){ ['#nodes', '#edges'].forEach(q => { $(q).style.transform = `translate(${W.pan.x}px,${W.pan.y}px) scale(${W.k || 1})`; }); }
function zoomBy(f, cx, cy){
  const c = $('#canvas').getBoundingClientRect(); cx = cx ?? c.width / 2; cy = cy ?? c.height / 2;
  const k0 = W.k || 1, uz = Math.max(.35, Math.min(2.5, (W.uz || 1) * f)); const k1 = k0 * uz / (W.uz || 1);
  W.pan = {x: cx - (cx - W.pan.x) * k1 / k0, y: cy - (cy - W.pan.y) * k1 / k0};           // zoom about the pointer
  W.uz = uz; W.k = k1; applyView(); saveView();
}
function resetLayout(){ W.manual.clear(); W.pan = {x: 0, y: 0}; W.uz = 1; saveView(); layoutAll(true); toast('Layout reset'); }
(function canvasInput(){
  const cv = document.getElementById('canvas'); let drag = null;
  cv.addEventListener('pointerdown', ev => {
    if (ev.button !== 0 || ev.target.closest('#canvas-tools')) return;
    const node = ev.target.closest('.node'); W.dragged = false;
    if (node && node.dataset.id && !node.classList.contains('ghost')) {
      const id = node.dataset.id, p = W.pos.get(id); if (!p) return;
      drag = {kind: 'node', id, el: node, x0: ev.clientX, y0: ev.clientY, px: p.x, py: p.y};
    } else if (!node) drag = {kind: 'pan', x0: ev.clientX, y0: ev.clientY, px: W.pan.x, py: W.pan.y};
    if (drag) drag.pid = ev.pointerId;                   // capture only once it is a drag, so a plain click still reaches the card
  });
  cv.addEventListener('pointermove', ev => {
    if (!drag) return; const dx = ev.clientX - drag.x0, dy = ev.clientY - drag.y0;
    if (!W.dragged && Math.hypot(dx, dy) < 4) return;
    if (!W.dragged) { W.dragged = true; try { cv.setPointerCapture(drag.pid); } catch (_) {} cv.classList.add(drag.kind === 'node' ? 'dragging' : 'panning'); if (drag.el) { drag.el.style.transition = 'none'; drag.el.classList.add('lifted'); } }
    if (drag.kind === 'node') {
      const k = W.k || 1, x = drag.px + dx / k, y = drag.py + dy / k, p = W.pos.get(drag.id);
      W.pos.set(drag.id, {...p, x, y}); W.manual.set(drag.id, {x: Math.round(x), y: Math.round(y)});
      drag.el.style.transform = `translate(${x}px,${y}px)`; redrawEdges();
    } else { W.pan = {x: drag.px + dx, y: drag.py + dy}; applyView(); }
  });
  const end = () => {
    if (!drag) return;
    if (drag.el) { drag.el.style.transition = ''; drag.el.classList.remove('lifted'); }
    cv.classList.remove('dragging', 'panning'); if (W.dragged) saveView();
    drag = null; setTimeout(() => { W.dragged = false; }, 0);                   // the click that ends a drag is not a click
  };
  cv.addEventListener('pointerup', end); cv.addEventListener('pointercancel', end);
  cv.addEventListener('wheel', ev => { if (!ev.ctrlKey && !ev.metaKey) return; ev.preventDefault(); const r = cv.getBoundingClientRect(); zoomBy(ev.deltaY < 0 ? 1.1 : 1 / 1.1, ev.clientX - r.left, ev.clientY - r.top); }, {passive: false});
})();

/* ------------------------------------------------------------------ attach a feed to a camera (RTSP, upload, webcam, snapshot URL, sample) */
const AT = {name: null, tab: 'rtsp', uploaded: '', uploadedName: '', sample: '', samples: null, tested: ''};
function srcTab(src){ const k = camKind(src); return {'RTSP camera': 'rtsp', 'snapshot URL': 'http', 'webcam': 'webcam', 'sample footage': 'sample', 'recording': 'upload', 'still image': 'upload'}[k] || 'rtsp'; }
async function openAttach(name){
  const cam = name ? W.cams.get(name) : null;
  AT.name = name; AT.uploaded = ''; AT.uploadedName = ''; AT.tested = ''; AT.sample = '';
  $('#at-title').textContent = cam ? name : 'Connect a camera';
  $('#at-kicker').textContent = cam ? (cam.source ? 'Camera feed · ' + camKind(cam.source) : 'Camera feed · not connected') : 'New camera';
  $('#at-name-row').classList.toggle('hide', !!cam); $('#at-cam-name').value = '';
  ['#at-rtsp', '#at-http'].forEach(q => { $(q).value = ''; });
  const src = cam && cam.source || '';
  if (src) { const t = srcTab(src); if (t === 'rtsp') $('#at-rtsp').value = src; else if (t === 'http') $('#at-http').value = src; else if (t === 'webcam') $('#at-webcam').value = src; else if (t === 'upload') { AT.uploaded = src; AT.uploadedName = src.split(/[\\/]/).pop(); } else if (t === 'sample') AT.sample = src; }
  atPreview(cam && cam.id ? `/api/cameras/${cam.id}/frame.jpg?t=${Date.now()}` : '', cam && cam.id ? 'Latest frame from this camera.' : '');
  $('#at-live').classList.toggle('hide', !(cam && cam.id));
  $('#at-go').textContent = cam && cam.source ? 'Switch feed' : 'Attach feed';
  atTab(src ? srcTab(src) : 'rtsp');
  $('#attach').classList.remove('hide');
  setTimeout(() => (cam ? ($('#at-' + AT.tab) || {focus(){}}) : $('#at-cam-name')).focus(), 50);
  if (!AT.samples) loadSamples();
}
function closeAttach(){ $('#attach').classList.add('hide'); }
function atTab(k){
  AT.tab = k;
  document.querySelectorAll('#at-tabs button').forEach(b => b.classList.toggle('on', b.dataset.k === k));
  document.querySelectorAll('#attach .pane').forEach(p => p.classList.toggle('hide', p.dataset.k !== k));
  if (k === 'upload') atUploadLabel();
  $('#at-test').classList.toggle('hide', k === 'sample' && !AT.sample);
}
function atUploadLabel(){ const d = $('#at-drop'); d.classList.toggle('done', !!AT.uploaded); d.querySelector('b').textContent = AT.uploaded ? AT.uploadedName : 'Drop a video or image here'; d.querySelector('span').textContent = AT.uploaded ? 'uploaded · click to choose a different file' : 'or click to choose a file'; }
function atPreview(src, msg, bad){
  const pv = $('#at-pv'); pv.innerHTML = src ? `<img src="${esc(src)}" alt="" onerror="this.remove()">` : '<span>Test the source to see a frame from it.</span>';
  const m = $('#at-msg'); m.textContent = msg || ''; m.className = 'pmsg' + (bad ? ' bad' : (src ? ' ok' : ''));
}
function atSource(){
  if (AT.tab === 'rtsp') return $('#at-rtsp').value.trim();
  if (AT.tab === 'http') return $('#at-http').value.trim();
  if (AT.tab === 'webcam') return $('#at-webcam').value;
  if (AT.tab === 'upload') return AT.uploaded;
  return AT.sample;
}
function atCheck(src){
  if (!src) return {upload: 'Choose a file first.', sample: 'Pick a clip first.', rtsp: 'Enter the stream address.', http: 'Enter the snapshot URL.'}[AT.tab] || 'Pick a source.';
  if (AT.tab === 'rtsp' && !/^rtsps?:\/\//i.test(src)) return 'An RTSP address starts with rtsp:// (for example rtsp://user:password@192.168.1.20:554/...).';
  if (AT.tab === 'http' && !/^https?:\/\//i.test(src)) return 'A snapshot URL starts with http:// or https://';
  return '';
}
async function loadSamples(){
  const r = await api('/cameras'); AT.samples = (r && r.samples) || [];
  const box = $('#at-samples');
  box.innerHTML = AT.samples.length ? AT.samples.map(c => `<button data-s="sample:${esc(c.name)}" onclick="pickSample(this.dataset.s)"><span>${esc(c.label)}</span><span class="c">${esc(c.name)}</span></button>`).join('') : '<div class="hint">No sample footage is installed on this desk.</div>';
  if (AT.sample) markSample();
}
function markSample(){ document.querySelectorAll('#at-samples button').forEach(b => b.classList.toggle('on', b.dataset.s === AT.sample || (AT.sample && !AT.sample.startsWith('sample:') && AT.sample.includes(b.dataset.s.slice(7))))); }
function pickSample(s){ AT.sample = s; markSample(); $('#at-test').classList.remove('hide'); testAttach(); }
function uploadAttach(file){
  if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const xhr = new XMLHttpRequest(); xhr.open('POST', '/api/cameras/upload');
  $('#at-prog').classList.remove('hide'); $('#at-bar').style.width = '0%'; $('#at-ptext').textContent = `uploading ${file.name}`;
  xhr.upload.onprogress = e => { if (e.lengthComputable) { const pc = Math.round(e.loaded * 100 / e.total); $('#at-bar').style.width = pc + '%'; $('#at-ptext').textContent = `uploading ${file.name} · ${pc}%`; } };
  xhr.onload = () => {
    let r = {}; try { r = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
    if (xhr.status === 401) { location.href = '/login?next=/desk/workspace'; return; }
    if (xhr.status >= 400 || r.error) { $('#at-prog').classList.add('hide'); atPreview('', r.error || `upload failed (${xhr.status})`, true); return; }
    AT.uploaded = r.source; AT.uploadedName = file.name; atUploadLabel();
    $('#at-ptext').textContent = `${file.name} · ${(r.bytes / 1048576).toFixed(1)} MB uploaded`;
    testAttach();
  };
  xhr.onerror = () => { $('#at-prog').classList.add('hide'); atPreview('', 'upload failed: the connection dropped', true); };
  xhr.send(fd);
}
async function testAttach(){
  const src = atSource(); const bad = atCheck(src); if (bad) return atPreview('', bad, true);
  $('#at-test').disabled = true; atPreview('', AT.tab === 'rtsp' ? 'Connecting to the stream…' : 'Reading a frame…');
  const r = await api('/cameras/probe', {method: 'POST', body: {source: src}});
  $('#at-test').disabled = false;
  if (!r || !r.ok) return atPreview('', (r && r.error) || 'no answer from the source', true);
  AT.tested = src; atPreview(r.preview, `Connected · ${camKind(r.source || src)}`);
}
async function doAttach(){
  const src = atSource(); const bad = atCheck(src); if (bad) return atPreview('', bad, true);
  let name = AT.name;
  if (!name) { name = $('#at-cam-name').value.trim().toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9_-]/g, ''); if (!name) { $('#at-cam-name').focus(); return atPreview('', 'Name the camera first, e.g. front-door.', true); } if (W.cams.has(name)) return atPreview('', `There is already a camera called ${name}.`, true); }
  if (AT.tested !== src) { await testAttach(); if (AT.tested !== src) return; }       // never attach an untested source
  $('#at-go').disabled = true;
  try {
    if (W.deskId) {
      const k = W.cams.get(name);
      const r = await api('/cameras/attach', {method: 'POST', body: {name, source: src, cid: k && k.id || undefined}});
      if (!r || r.error) return atPreview('', (r && r.error) || 'could not attach', true);
      if (!k) await applyCams([...[...W.cams.values()].map(c => ({name: c.name, source: c.source, journal: c.journal, alerts: c.alerts, id: c.id})), {name, source: src, journal: true, id: r.camera.id}], false);
      const nk = W.cams.get(name); Object.assign(nk, {id: r.camera.id, source: src, seenTs: 0}); const kd = nk.el.querySelector('.kind'); if (kd) kd.textContent = camKind(src);
      setCamState(nk, 'starting…', 'on'); if (W.phase === 'run') { startCamPoll(); setSugg(CAM_QUESTIONS); }
      addMsg('s', `camera ${name} → ${camKind(src)}`, 'assign');
    } else {                                                                            // not built yet: it goes into the draft
      if (!W.bp) W.bp = {agents: [], cameras: []};
      const list = W.bp.cameras = W.bp.cameras || []; const c = list.find(x => x.name === name);
      if (c) c.source = src; else list.push({name, source: src, journal: true, notes: '', focus: ''});
      W.addedCams.set(name, src);
      if (W.phase === 'meet') await openWorkspace();
      await applyCams(list);
      const kd = W.cams.get(name).el.querySelector('.kind'); if (kd) kd.textContent = camKind(src);
      addMsg('s', `camera ${name} → ${camKind(src)} (goes live when the desk is built)`, 'assign');
      $('#build-btn').disabled = !((W.bp.agents || []).length);
    }
    toast(`${name}: ${camKind(src)} attached`); closeAttach();
  } finally { $('#at-go').disabled = false; }
}
function atLive(){ const n = AT.name; closeAttach(); if (n) openLive(n); }
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#attach').classList.contains('hide')) closeAttach(); });
(function dropZone(){
  const d = document.getElementById('at-drop'); if (!d) return;
  ['dragenter', 'dragover'].forEach(t => d.addEventListener(t, e => { e.preventDefault(); d.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => d.addEventListener(t, e => { e.preventDefault(); d.classList.remove('over'); }));
  d.addEventListener('drop', e => { const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (f) uploadAttach(f); });
})();

/* ------------------------------------------------------------------ console: explorer, KPIs, team table, run panel, tabs (direction D "Foundry") */
const fmtDur = s => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
function agentState(id){
  if (id === 'atlas') return W.run && W.run.active ? {cls: 'run', dot: 'd-run', label: 'Coordinating', turns: ''} : {cls: 'q', dot: W.phase === 'run' ? 'd-ok' : 'd-q', label: W.phase === 'run' ? 'Lead · idle' : 'Lead', turns: ''};
  if (W.run) { const st = [...W.run.inst.values()].filter(s => s.base === id); if (st.length) { const s = st[st.length - 1];
    const map = {writing: ['run', 'd-run', 'Writing'], thinking: ['run', 'd-think', 'Thinking'], done: ['ok', 'd-ok', 'Done'], error: ['err', 'd-err', 'Error'], idle: ['q', 'd-q', 'Queued']};
    const [cls, dot, label] = map[s.status] || ['q', 'd-q', s.status]; return {cls, dot, label: label + (s.turns ? ' · turn ' + s.turns : ''), turns: s.turns}; } }
  return {cls: 'q', dot: W.phase === 'run' ? 'd-ok' : 'd-q', label: W.phase === 'run' ? 'Idle' : 'Drafted', turns: ''};
}
function renderAll(){ renderExplorer(); renderKpis(); renderTeamTable(); renderHead(); }
function renderHead(){
  const n = W.agents.size; const built = !!W.deskId;
  $('#ph-title').textContent = W.deskName || 'New operation'; $('#bz-name').textContent = W.deskName || 'New operation';
  $('#ph-k').textContent = built ? `Desk #${W.deskId} · ${(W.template || 'custom').replace(/_/g, ' ')} · ${W.tier === 'free' ? 'free' : 'paid'} models${W.cams.size ? ` · ${W.cams.size} camera${W.cams.size === 1 ? '' : 's'}` : ''}`
    : (n ? `Draft · ${n} agent${n === 1 ? '' : 's'} · build to go live` : 'Atlas workspace · describe the business to draft a desk');
  const chips = [];
  if (built) chips.push(`<span class="chip c-green"><i class="dot d-ok"></i>Built</span>`); else if (n) chips.push(`<span class="chip c-blue">Draft</span>`);
  if (W.run) chips.push(W.run.active ? `<span class="chip c-blue"><i class="dot d-run"></i>${esc(W.run.id)} running</span>` : `<span class="chip c-grey">${esc(W.run.id)} ${esc(W.run.status || 'done')}</span>`);
  if (W.cams.size) chips.push(`<span class="chip">${W.cams.size} camera${W.cams.size === 1 ? '' : 's'}${[...W.cams.values()].some(c => c.journal) ? ' · journal on' : ''}</span>`);
  $('#ph-chips').innerHTML = chips.join('');
  $('#tab-n-team').textContent = n ? n + 1 : 0; $('#tab-n-cams').textContent = W.cams.size;
  $('#team-meta').textContent = n ? `Atlas leads · ${n} specialist${n === 1 ? '' : 's'} report${n === 1 ? 's' : ''} to Atlas` : 'drafted as you talk to Atlas';
  $('#cam-meta').textContent = W.cams.size ? `${W.cams.size} feed${W.cams.size === 1 ? '' : 's'} · each keeps a written journal` : 'each camera keeps a written journal';
  $('#start-panel').classList.toggle('hide', n > 0 || W.phase === 'run');
  $('#tab-upd').textContent = 'Updated ' + new Date().toISOString().slice(11, 19) + ' UTC';
}
function kpi(sel, v, small, s, cls){ const el = $(sel); if (!el) return; el.className = 'kpi' + (cls ? ' ' + cls : ''); el.querySelector('.v').innerHTML = esc(String(v)) + (small ? `<small>${esc(small)}</small>` : ''); if (s !== undefined) el.querySelector('.s').textContent = s; }
function renderKpis(){
  const n = W.agents.size ? W.agents.size + 1 : 0;
  const working = W.run && W.run.active ? [...W.run.inst.values()].filter(s => ['writing', 'thinking'].includes(s.status)).length : 0;
  kpi('#k-agents', n, '', !n ? 'no team drafted yet' : (W.run && W.run.active ? `${working} working now` : (W.phase === 'run' ? 'idle · ready for a job' : 'drafted · not built yet')));
  const cams = [...W.cams.values()]; const live = cams.filter(c => c.seenTs).length;
  kpi('#k-cams', live, `of ${cams.length}`, !cams.length ? 'none attached' : (W.phase !== 'run' ? 'go live when the desk is built' : (cams.some(c => c.journal) ? 'YOLO + VLM journal' : 'rules only')), cams.length && !live && W.phase === 'run' ? 'warn' : '');
  kpi('#k-journal', W.journalCount ?? '-', '', W.journalLast ? 'last journal note ' + W.journalLast + ' UTC' : (cams.length ? 'detections · no journal note yet' : 'journal off'));
  kpi('#k-ap', W.apCount || 0, '', W.apCount ? 'oldest ' + (W.apOldest || '') : 'nothing outbound queued', W.apCount ? 'warn' : '');
  if (!W.spend) { const el = $('#k-spend'); el.querySelector('.v').innerHTML = W.tier === 'free' ? '$0.00' : '-'; el.querySelector('.s').innerHTML = W.tier === 'free' ? '<span class="bar"><i style="width:0%"></i></span>free models' : 'spend unknown'; }
}
function renderTeamTable(){
  const rows = []; const ids = W.agents.size ? ['atlas', ...W.order] : [];
  for (const id of ids) {
    const a = id === 'atlas' ? {name: 'Atlas', role: 'Orchestrator · engagement lead', tools: ['delegate', 'queue_action', 'crm_update', 'save_deliverable'], engine: 'atlas', color: ''} : W.agents.get(id); if (!a) continue;
    const st = agentState(id); const lead = id === 'atlas' || (a.members || []).length > 0;
    const member = a.reports_to && a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? (W.agents.get(a.reports_to) || {}).name : '';
    const tools = a.tools || [];
    rows.push(`<tr id="tr-${esc(id)}" class="${W.sel === id ? 'sel' : ''}" onclick="inspect(${JSON.stringify(id).replace(/"/g, '&quot;')})"><td><div class="obj ${id === 'atlas' ? 'lead' : ''}" style="--c:${esc(a.color || '')}"><span class="ic"><svg class="i i12"><use href="#${id === 'atlas' ? 'i-logo' : (lead ? 'i-team' : 'i-agent')}"/></svg></span><div><b>${esc(a.name)}</b><span>${esc(a.role || '')}${member ? ' · reports to ' + esc(member) : ''}</span></div></div></td>` +
      `<td><span class="st ${st.cls}"><i class="dot ${st.dot}"></i>${esc(st.label)}</span></td><td class="t2">${esc(a.engine === 'hermes_agent' ? 'hermes_agent' : (a.engine || 'atlas'))}</td><td class="num">${st.turns || '<span class="t3">-</span>'}</td>` +
      `<td><div class="tools">${tools.slice(0, 4).map(t => `<code>${esc(t)}</code>`).join('')}${tools.length > 4 ? `<code>+${tools.length - 4}</code>` : ''}</div></td></tr>`);
  }
  $('#team-rows').innerHTML = rows.join(''); $('#team-empty').classList.toggle('hide', rows.length > 0);
}
function teamView(v){
  W.teamView = v; try { localStorage.setItem('ws_teamview', v); } catch (_) {}
  document.querySelectorAll('#team-view button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  $('#team-table').classList.toggle('hide', v === 'chart'); $('#canvas').classList.toggle('hide', v !== 'chart'); $('#canvas-tools').classList.toggle('hide', v !== 'chart');
  if (v === 'chart') requestAnimationFrame(() => { if (W.phase !== 'meet') layoutAll(false); });
}
function renderRunPanel(){
  const body = $('#run-body'); const r = W.run;
  if (!r) { $('#run-id').textContent = ''; $('#run-meta').textContent = 'no run yet'; return; }
  $('#run-id').textContent = r.id; $('#run-meta').textContent = r.active ? 'running' : (r.status || 'finished');
  const insts = [...r.inst.values()].filter(s => s.base !== 'atlas');
  const steps = insts.map((s, i) => { const cls = s.status === 'done' ? 'ok' : (s.status === 'error' ? 'err' : (['writing', 'thinking'].includes(s.status) ? 'run' : '')); const name = (W.agents.get(s.base) || {}).name || s.base;
    const sub = s.status === 'done' ? (s.turns ? `${s.turns} turn${s.turns === 1 ? '' : 's'}` : 'done') : (s.status === 'writing' ? `writing · turn ${s.turns}` : (s.status === 'thinking' ? (s.turns ? `turn ${s.turns}` : 'briefed') : (s.status === 'error' ? 'error' : 'queued')));
    return `<div class="step ${cls}"><span class="sno">${cls === 'ok' ? '<svg class="i i12"><use href="#i-check"/></svg>' : i + 1}</span><span class="nm">${esc(name)}${s.ghost ? ` <span>${esc(s.inst)}</span>` : ''}<span>${esc(sub)}</span></span><span class="tm" title="${esc(s.assignment || '')}">${esc((s.assignment || '').slice(0, 42))}</span></div>`; });
  const live = insts.filter(s => s.status === 'writing' && s.text).pop() || insts.filter(s => s.text).pop();
  body.innerHTML = `<div class="run-task"><b>Job</b> ${esc(r.task || '')}</div><div class="steps">${steps.join('') || `<div class="step run"><span class="sno">1</span><span class="nm">Atlas<span>briefing the team</span></span><span class="tm"></span></div>`}</div>`
    + (live ? `<div class="out-box" id="run-live" data-inst="${esc(live.inst)}"><span class="who">${esc((W.agents.get(live.base) || {}).name || live.base)}</span><span class="tx">${esc(live.text.slice(-360))}</span>${r.active ? '<span class="caret"></span>' : ''}</div>` : '')
    + `<div class="rmeta" id="run-rmeta"></div>`;
  renderRunMeta();
}
function renderRunLive(s){ const box = $('#run-live'); if (!box || box.dataset.inst !== s.inst) { if (!W.liveRaf) W.liveRaf = requestAnimationFrame(() => { W.liveRaf = 0; renderRunPanel(); }); return; } box.querySelector('.tx').textContent = s.text.slice(-360); }
function renderRunMeta(){
  const r = W.run; const el = $('#run-rmeta'); if (!r) return;
  const secs = Math.max(0, Math.round(((r.ended || Date.now()) - r.started) / 1000));
  if (el) el.innerHTML = `<span>Elapsed <b>${fmtDur(secs)}</b></span><span>Tokens <b>${(r.tin || 0).toLocaleString()}</b> in · <b>${(r.tout || 0).toLocaleString()}</b> out</span>${r.status ? `<span>Status <b>${esc(r.status)}</b></span>` : ''}`;
  $('#sb-run-t').textContent = fmtDur(secs);
}
setInterval(() => { if (W.run && W.run.active) renderRunMeta(); }, 1000);
async function cancelRun(){ if (!W.run || !W.run.active) return; const r = await api(`/runs/${encodeURIComponent(W.run.id)}/cancel`, {method: 'POST', body: {}}); toast(r && !r.error ? 'Stopping the run' : ((r && r.error) || 'could not stop')); }
async function loadRuns(){
  if (!W.deskId) return;
  const rows = await api('/runs'); if (!Array.isArray(rows)) return;
  $('#runs-meta').textContent = rows.length ? `${rows.length} run${rows.length === 1 ? '' : 's'}` : '';
  $('#runs-list').innerHTML = rows.length ? rows.slice(0, 40).map(r => { const dot = r.active ? 'd-run' : (r.status === 'done' ? 'd-ok' : (r.status === 'error' || r.status === 'failed' ? 'd-err' : 'd-q'));
    return `<div class="rrow" onclick="openRun('${esc(r.id)}')"><i class="dot ${dot}"></i><span class="nm" title="${esc(r.task || '')}">${esc(r.task || '')}</span><span class="mono">${esc(r.active ? 'running' : (r.status || ''))}</span><span class="mono">${((r.tokens_in || 0) + (r.tokens_out || 0)).toLocaleString()} tok</span><span class="mono">${new Date((r.created || 0) * 1000).toISOString().slice(5, 16).replace('T', ' ')}</span></div>`; }).join('')
    : '<div class="empty sm"><span>Runs appear here once the team has had a job.</span></div>';
}
async function openRun(id){
  const r = await api(`/runs/${encodeURIComponent(id)}`); if (!r || r.error) return toast((r && r.error) || 'run not found');
  const text = (r.summary || '').trim() + (r.deliverables || []).map(d => `\n\n### ${d.name}\n\n${d.content}`).join('');
  $('#summary-text').innerHTML = md(text || '(no summary yet)'); $('#summary').classList.remove('hide'); $('#summary-empty').classList.add('hide');
  $('#sum-meta').textContent = `${id} · ${r.active ? 'running' : (r.status || '')}`;
}
async function loadJournal(){
  if (!W.deskId) return;
  const ev = await api('/vision/events?hours=24&limit=40'); const box = $('#journal-list'); if (!Array.isArray(ev)) return;
  const notes = ev.filter(e => e.answer).sort((a, b) => b.ts - a.ts);
  $('#jr-meta').textContent = notes.length ? `${notes.length} notes · last 24 h` : 'last 24 hours';
  box.innerHTML = notes.length ? notes.map(e => `<div class="jrow"><span class="ts">${new Date(e.ts * 1000).toISOString().slice(11, 16)}</span><div><span class="cm">${esc(e.camera)}${e.source === 'digest' ? ' · summary' : ''}</span><div class="tx">${esc(e.answer)}</div></div></div>`).join('') : '<div class="empty sm"><span>Journal notes appear here once a camera is live.</span></div>';
}
function showTab(name){
  W.tab = name;
  document.querySelectorAll('#tabs .tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  document.querySelectorAll('.tpane').forEach(p => p.classList.toggle('hide', p.id !== 'pane-' + name));
  const tp = $('#team-panel'); if (name === 'team') $('#team-big').appendChild(tp); else if (tp.parentElement !== $('#pane-overview')) $('#pane-overview').insertBefore(tp, $('#run-panel'));
  const cams = $('#cams'); cams.classList.toggle('big', name === 'cameras'); if (name === 'cameras') $('#cam-big').appendChild(cams); else if (cams.parentElement !== $('#camstrip')) $('#camstrip').appendChild(cams);
  const ap = $('#ap-panel'); if (name === 'approvals') $('#ap-big').appendChild(ap); else if (ap.parentElement !== $('#right')) $('#right').insertBefore(ap, $('#act-panel'));
  if (name === 'cameras') loadJournal(); if (name === 'runs') loadRuns(); if (name === 'approvals') loadApprovals();
  if (name === 'team' && W.teamView === 'chart') requestAnimationFrame(() => { if (W.phase !== 'meet') layoutAll(false); });
}
function newJob(){ showTab('runs'); if (isPhone()) mtab('runs'); setTimeout(() => $('#job').focus({preventScroll: true}), 60); }
function clearFeed(){ $('#feed').innerHTML = ''; }
function exFilter(f){ W.exf = f; document.querySelectorAll('#ex-seg button').forEach(b => b.classList.toggle('on', b.dataset.f === f)); renderExplorer(); }
function renderExplorer(){
  const q = ($('#ex-filter').value || '').trim().toLowerCase(); const f = W.exf || 'all';
  const match = s => !q || String(s).toLowerCase().includes(q);
  const out = [];
  const grp = (icon, name, n, meta, rows) => { if (!rows.length) return; out.push(`<div class="grp"><svg class="i i12 ch"><use href="#i-down"/></svg><svg class="i i14"><use href="#${icon}"/></svg>${name}<span class="n">${n}</span><span class="m">${esc(meta || '')}</span></div>${rows.join('')}`); };
  const row = (icon, name, meta, metaCls, onclick, sel, extra) => `<div class="row ${sel ? 'sel' : ''}" onclick="${onclick}"><svg class="i"><use href="#${icon}"/></svg><span class="nm">${esc(name)}</span>${extra || ''}<span class="m ${metaCls || ''}">${esc(meta || '')}</span></div>`;
  // agents
  const ids = W.agents.size ? ['atlas', ...W.order] : [];
  const arows = []; let working = 0, errors = 0;
  for (const id of ids) { const a = id === 'atlas' ? {name: 'Atlas'} : W.agents.get(id); if (!a) continue; const st = agentState(id); if (st.cls === 'run') working++; if (st.cls === 'err') errors++;
    if (!match(a.name) || (f === 'active' && st.cls !== 'run') || (f === 'attention' && st.cls !== 'err')) continue;
    arows.push(row(id === 'atlas' ? 'i-logo' : 'i-agent', a.name, st.label.toLowerCase(), st.cls === 'run' ? 'run' : (st.cls === 'err' ? 'warn' : ''), `inspect(${JSON.stringify(id).replace(/"/g, '&quot;')})`, W.sel === id, id === 'atlas' ? '<span class="chip">LEAD</span>' : '')); }
  grp('i-team', 'Agents', ids.length, working ? `${working} working` : (errors ? `${errors} error${errors === 1 ? '' : 's'}` : (W.phase === 'run' ? 'idle' : (ids.length ? 'draft' : ''))), arows);
  // cameras
  const crows = []; let liveN = 0, noSrc = 0;
  for (const [name, k] of W.cams) { const live = !!k.seenTs; if (live) liveN++; if (!k.source) noSrc++;
    if (!match(name) || (f === 'active' && !live) || (f === 'attention' && k.source)) continue;
    const counts = k.el && k.el.querySelector('.cnt') ? k.el.querySelector('.cnt').textContent : '';
    crows.push(row('i-cam', name, live ? (counts || 'live') : (k.source ? camKind(k.source) : 'no source'), live ? 'ok' : (k.source ? '' : 'warn'), `camRow(${JSON.stringify(name).replace(/"/g, '&quot;')})`, false)); }
  grp('i-cam', 'Cameras', W.cams.size, liveN ? `${liveN} live` : (noSrc ? `${noSrc} need a source` : (W.cams.size ? 'journal on' : '')), crows);
  // workflows
  if (f === 'all') { const wrows = (W.workflows || []).filter(w => match(w.name)).map(w => row('i-flow', w.name, 'run', '', `runWorkflow(${JSON.stringify(w.name).replace(/"/g, '&quot;')})`, false)); grp('i-flow', 'Workflows', (W.workflows || []).length, '', wrows); }
  // approvals
  if (W.apCount && f !== 'active' && match('approvals')) grp('i-inbox', 'Approvals', W.apCount, 'oldest ' + (W.apOldest || ''), [row('i-inbox', `${W.apCount} waiting for you`, '', 'warn', "showTab('approvals')", false)]);
  // desks
  if (f === 'all') { const drows = (W.desks || []).filter(d => match(d.business_name || d.name)).map(d => row('i-desk', d.business_name || d.name, '#' + d.id, '', `openDesk(${d.id})`, W.deskId === d.id)); grp('i-desk', 'Desks', (W.desks || []).length, '', drows); }
  $('#tree').innerHTML = out.join('') || '<div class="empty sm"><span>Nothing matches.</span></div>';
  const hj = $('#h-journal'); if (hj) hj.innerHTML = W.cams.size ? `<i class="dot ${liveN ? 'd-ok' : 'd-warn'}"></i>${[...W.cams.values()].some(c => c.journal) ? 'on' : 'off'} <span class="mono">${liveN} / ${W.cams.size} cams</span>` : 'no cameras';
}
function camRow(name){ const k = W.cams.get(name); if (!k) return; if (k.id && k.seenTs) openLive(name); else openAttach(name); }
function runWorkflow(name){ $('#job').value = `Run workflow '${name}'`; newJob(); }
async function pollHealth(){
  if (!W.deskId) return;
  const h = await api('/health/full'); if (!h || h.error) return;
  W.spend = h.spend || null; const sp = W.spend || {}; const el = $('#k-spend');
  if (sp.used_usd != null) { const cap = sp.cap_usd; const pct = cap ? Math.min(100, Math.round(sp.used_usd / cap * 100)) : 0; const cls = sp.blocked ? 'bad' : (cap && pct >= 80 ? 'warn' : '');
    el.className = 'kpi' + (cls ? ' ' + cls : ''); el.querySelector('.v').innerHTML = `$${sp.used_usd.toFixed(2)}${cap ? `<small>/ $${cap.toFixed(2)}</small>` : ''}`;
    el.querySelector('.s').innerHTML = `<span class="bar"><i style="width:${pct}%"></i></span>${sp.blocked ? 'cap reached - runs blocked' : (cap ? pct + '% of cap' : 'no cap set')}`;
    $('#sb-spend').classList.remove('hide'); $('#sp-f').style.width = pct + '%'; $('#sp-v').textContent = `$${sp.used_usd.toFixed(2)}`; $('#sb-spend .meter').className = 'meter' + (sp.blocked ? ' over' : (pct >= 80 ? ' near' : '')); }
  else { W.spend = null; renderKpis(); }
  const alerts = h.alerts || []; const top = alerts.find(a => a.level === 'critical') || alerts[0];
  const b = $('#banner'); if (top) { b.classList.remove('hide'); b.classList.toggle('bad', top.level === 'critical'); $('#banner-msg').textContent = top.text; } else b.classList.add('hide');
  if (h.queue) { W.apCount = h.queue.pending; }
}
setInterval(() => { if (W.deskId) pollHealth(); }, 60000);
/* command palette */
function palItems(){
  const q = ($('#pal-q').value || '').toLowerCase().trim(); const it = [];
  const add = (g, icon, label, s, fn) => { if (!q || (label + ' ' + s).toLowerCase().includes(q)) it.push({g, icon, label, s, fn}); };
  if (W.phase === 'run') add('Actions', 'i-plus', 'New job', 'N', () => newJob());
  add('Actions', 'i-cam', 'Add camera', '', () => openAttach(null));
  if (W.bp && !W.deskId && (W.bp.agents || []).length) add('Actions', 'i-build', 'Build desk', '', () => buildDesk());
  add('Actions', 'i-inbox', 'Approvals', W.apCount ? W.apCount + ' waiting' : '', () => toggleApprovals());
  add('Actions', 'i-logo', 'Ask Atlas', '/', () => { $('#say').focus(); if (isPhone()) mtab('atlas'); });
  add('Actions', 'i-help', 'How Atlas works', '', () => openGuide());
  add('Actions', 'i-reset', 'Reset org chart layout', '', () => resetLayout());
  add('Actions', 'i-sparkle', W.tier === 'free' ? 'Switch to paid models' : 'Switch to free models', '', () => toggleTier());
  [['overview', 'Overview'], ['team', 'Team'], ['cameras', 'Cameras'], ['runs', 'Runs'], ['approvals', 'Approvals']].forEach(([t, l]) => add('Go to', 'i-grid', l, 'tab', () => { showTab(t); if (isPhone()) mtab(t === 'team' ? 'overview' : t); }));
  add('Go to', 'i-book', "Today's journal", 'opens a page', () => window.open($('#journal-link').href, '_blank'));
  add('Go to', 'i-desk', 'Full dashboard', '/desk', () => { location.href = '/desk'; });
  W.agents.forEach(a => add('Agents', 'i-agent', a.name, a.role || '', () => inspect(a.id)));
  if (W.agents.size) add('Agents', 'i-logo', 'Atlas', 'orchestrator', () => inspect('atlas'));
  W.cams.forEach(c => add('Cameras', 'i-cam', c.name, c.seenTs ? 'live view' : camKind(c.source), () => camRow(c.name)));
  (W.desks || []).forEach(d => add('Desks', 'i-desk', d.business_name || d.name, '#' + d.id, () => openDesk(d.id)));
  return it;
}
function openPalette(){ $('#palette').classList.remove('hide'); $('#pal-q').value = ''; W.palI = 0; renderPalette(); setTimeout(() => $('#pal-q').focus(), 30); }
function closePalette(){ $('#palette').classList.add('hide'); }
function renderPalette(){
  const it = W.pal = palItems(); if (W.palI >= it.length) W.palI = 0; let g = '';
  $('#pal-list').innerHTML = it.length ? it.map((x, i) => (x.g !== g ? `<div class="pal-g">${esc(g = x.g)}</div>` : '') + `<button class="pal-i ${i === W.palI ? 'on' : ''}" onmousemove="W.palI=${i};palMark()" onclick="palRun(${i})"><svg class="i i14"><use href="#${x.icon}"/></svg><span>${esc(x.label)}</span><span class="s">${esc(x.s)}</span></button>`).join('') : '<div class="empty sm"><span>Nothing matches.</span></div>';
}
function palMark(){ document.querySelectorAll('.pal-i').forEach((b, i) => b.classList.toggle('on', i === W.palI)); }
function palRun(i){ const x = (W.pal || [])[i]; closePalette(); if (x) x.fn(); }
function palKey(ev){ const n = (W.pal || []).length; if (ev.key === 'ArrowDown') { ev.preventDefault(); W.palI = (W.palI + 1) % Math.max(1, n); palMark(); const on = $('.pal-i.on'); if (on) on.scrollIntoView({block: 'nearest'}); } else if (ev.key === 'ArrowUp') { ev.preventDefault(); W.palI = (W.palI - 1 + n) % Math.max(1, n); palMark(); const on = $('.pal-i.on'); if (on) on.scrollIntoView({block: 'nearest'}); } else if (ev.key === 'Enter') { ev.preventDefault(); palRun(W.palI); } else if (ev.key === 'Escape') closePalette(); }
/* phone: one column + bottom bar; the activity feed joins the Runs tab */
function isPhone(){ return matchMedia('(max-width:900px)').matches; }
function mtab(name){
  document.body.dataset.m = name;
  document.querySelectorAll('#mnav button').forEach(b => b.classList.toggle('on', b.dataset.m === name));
  if (['overview', 'cameras', 'runs', 'approvals'].includes(name)) showTab(name);
  if (name === 'atlas') setTimeout(() => { $('#msgs').scrollTop = 1e9; }, 50);
}
function syncMobile(){
  const act = $('#act-panel');
  if (isPhone()) { if (act.parentElement !== $('#pane-runs')) $('#pane-runs').appendChild(act); if (!document.body.dataset.m) mtab('overview'); }
  else { if (act.parentElement !== $('#right')) $('#right').insertBefore(act, $('#atlas-panel')); delete document.body.dataset.m; document.querySelectorAll('#mnav button').forEach(b => b.classList.remove('on')); }
}
window.addEventListener('resize', () => { clearTimeout(W.mobT); W.mobT = setTimeout(syncMobile, 120); });
window.addEventListener('DOMContentLoaded', () => { syncMobile(); let tv = 'table'; try { tv = localStorage.getItem('ws_teamview') || 'table'; } catch (_) {} teamView(tv); renderAll(); });
