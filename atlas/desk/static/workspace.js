/* Atlas workspace, inside the shared app frame: talk to Atlas -> the team is drafted in front of you -> build -> give the
   team a job and watch every agent work, output streaming live. Everything on screen comes from real events (design
   turns, blueprint diffs, the run's SSE feed, the cameras' own status) and says honestly what it is. */
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const jsq = s => JSON.stringify(String(s ?? '')).replace(/"/g, '&quot;');       // a string inside an inline onclick
const sleep = ms => new Promise(r => setTimeout(r, ms));
const plural = (n, w, pl) => `${n} ${n === 1 ? w : (pl || w + 's')}`;

const W = {
  phase: 'meet',            // load -> run (a built desk) | meet -> design -> run (a new desk)
  sid: null, mode: 'demo', tier: 'free', liveReason: '',
  pin: 0,                   // the desk this page shows (?desk=N): every request names it, whatever other tabs select
  bp: null,                 // current blueprint (design phase)
  agents: new Map(),        // id -> {id,name,role,goal,instructions,tools,engine,engine_note,reports_to,members,color}
  order: [], pos: new Map(),
  deskId: null, deskName: '', template: '', workflows: [], cfgAgents: null,
  run: null,                // {id, es, inst: Map(instId -> state), active, status, ...}
  sel: null, busy: false, edgeAnim: null,
  cams: new Map(),          // name -> {name, id, source, journal, el, st, watching, frameTs, problem, lastNote, counts}
  camPoll: null, evSince: 0,
  manual: new Map(), pan: {x: 0, y: 0}, uz: 1,
  addedCams: new Map(),
  modeSel: 'job', apCount: null, apOldest: '', runs: [], desks: [], notes24: null, lastNoteTs: 0, cost: null, activeRuns: 0,
};

// every API call names this page's desk (X-Desk), so another tab switching desk never changes what this page reads
const deskOf = () => W.deskId || W.pin || 0;
const withDesk = u => deskOf() ? u + (u.includes('?') ? '&' : '?') + 'desk=' + deskOf() : u;
const api = async (p, opt) => {
  const headers = deskOf() ? {'X-Desk': String(deskOf())} : {};
  const init = opt ? {...opt, headers: {'Content-Type': 'application/json', ...headers}, body: opt.body ? JSON.stringify(opt.body) : undefined} : {headers};
  const r = await fetch('/api' + p, init);
  if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search); return null; }
  try { return await r.json(); } catch (_) { return {error: 'bad response ' + r.status}; }
};
function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('on'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('on'), 2800); }

/* ---- time: one rule for the whole page. Data times are UTC, labelled; anything not from today carries its date. */
const pad2 = n => String(n).padStart(2, '0');
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const sameUtcDay = (a, b) => a.getUTCFullYear() === b.getUTCFullYear() && a.getUTCMonth() === b.getUTCMonth() && a.getUTCDate() === b.getUTCDate();
const hhmmss = d => (d || new Date()).toISOString().slice(11, 19);
function fmtTs(ts, sec){                                   // ts in seconds -> "23:31" today, "23 Sep 23:31" otherwise (UTC)
  if (!ts) return ''; const d = new Date(ts * 1000), t = d.toISOString().slice(11, sec ? 19 : 16);
  return sameUtcDay(d, new Date()) ? t : `${d.getUTCDate()} ${MON[d.getUTCMonth()]}${d.getUTCFullYear() !== new Date().getUTCFullYear() ? ' ' + d.getUTCFullYear() : ''} ${t}`;
}
const fullTs = ts => ts ? new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : '';
function ago(ts){ if (!ts) return ''; const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? 'now' : s < 3600 ? Math.round(s / 60) + ' min' : s < 86400 ? Math.round(s / 3600) + ' h' : Math.round(s / 86400) + ' d'; }
function dayLabel(ts){ const d = new Date(ts * 1000); const l = d.toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'}); return sameUtcDay(d, new Date()) ? 'Today · ' + l : l; }
function countsText(c){
  const pl = (k, n) => n === 1 ? k : (k === 'person' ? 'people' : /(s|x|ch|sh)$/.test(k) ? k + 'es' : k + 's');
  return Object.entries(c || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${n} ${pl(k, n)}`).join(', ');
}

const PALETTE = ['#4c90f0', '#32a467', '#ec9a3c', '#9881f3', '#2ec4b6', '#e76a6e', '#68c1ee', '#d1980b', '#c274c2', '#8eb125'];   // one muted colour per agent
function agentName(id){ if (!id) return ''; if (id === 'atlas') return 'Atlas'; if (id === 'system') return 'Desk'; if (id === 'owner') return 'You'; const base = String(id).split('#')[0]; const a = W.agents.get(base); return a ? a.name + (base !== id ? ' #' + String(id).split('#')[1] : '') : id; }
function setState(t){ t = String(t || 'ready').toLowerCase(); const el = $('#chat-state'); el.textContent = t; el.className = 'tag' + (t !== 'ready' ? ' busy' : ''); }
function setPhase(t){ const e = $('#phase-label'); e.textContent = t; e.className = 'tag ' + (/^running/.test(t) ? 'running' : /ready|built/.test(t) ? 'done' : /fail|error/.test(t) ? 'error' : ''); }
function setPhaseAttr(){ $('#wk').dataset.phase = document.body.dataset.phase = W.phase; placePanels(); if (typeof msgsBottom === 'function') msgsBottom(); if (W.teamView !== viewFor()) applyTeamView(); else fitTeam(); }
function setDeskStatus(){
  const free = W.tier === 'free';
  $('#sb-desk').textContent = W.deskId ? '#' + W.deskId : (W.bp ? 'draft' : 'none');
  $('#sb-tier').textContent = free ? 'Free models' : 'Paid models';
  $('#tier-pill').className = 'pill' + (free ? '' : ' ok') + (W.sid && !W.deskId ? '' : ' hide');
  $('#h-models').textContent = free ? 'free tier' : 'paid tier';
  $('#as-sub').textContent = 'orchestrator · ' + (free ? 'free models' : 'paid models');
}
async function initStatus(){
  const live = W.mode === 'live' && !W.liveReason;
  const h = await api('/health') || {};
  const eng = h.hermes_agent ? 'Hermes Agent' : (h.ok ? 'built-in' : 'unreachable');
  const p = $('#eng-pill');                                   // Home's wording, so the two pages never disagree
  p.className = 'pill ' + (h.ok === false || W.liveReason ? 'bad' : live ? 'ok' : 'warn');
  $('#sb-engine').textContent = h.ok === false ? 'Engine unreachable' : W.liveReason ? 'Models unreachable' : live ? 'Live models' : 'Demo agents (scripted)';
  p.title = 'Agent engine: ' + eng + (W.liveReason ? ' · ' + W.liveReason : '') + (live ? '' : ' · demo mode: agents follow a script, no model calls');
  $('#sb-mode').textContent = eng + (live ? '' : ' · ' + (W.mode || 'demo'));
  $('#sb-dot').className = 'dot ' + (h.ok === false ? 'd-err' : (live ? 'd-ok' : 'd-warn'));
  setDeskStatus();
}

/* ------------------------------------------------------------------ boot */
window.addEventListener('DOMContentLoaded', boot);
async function boot(){
  const q = new URLSearchParams(location.search);
  W.pin = parseInt(q.get('desk') || '', 10) || 0;
  W.phase = $('#wk').dataset.phase || 'meet';                              // 'load' for a desk link: never a flash of the new-desk copy
  try { W.exClosed = new Set(JSON.parse(localStorage.getItem('ws_ex_closed') || '[]')); } catch (_) { W.exClosed = new Set(); }
  applySize();
  applyTeamView();
  if (window.ResizeObserver) new ResizeObserver(fitTeam).observe($('#team-panel'));
  if (window.ResizeObserver) new ResizeObserver(() => { cancelAnimationFrame(W.sizeRaf); W.sizeRaf = requestAnimationFrame(applySize); }).observe($('#wk'));
  else window.addEventListener('resize', applySize);
  setTimeout(() => { if (!document.getElementById('sh-rail')) document.body.classList.add('no-frame'); }, 3000);
  renderAll();
  const cfg = await api('/config') || {};
  W.mode = cfg.mode || 'demo'; W.liveReason = cfg.live_reason || '';
  initStatus();
  if (W.pin && cfg.desk) {
    if (cfg.desk.id !== W.pin) history.replaceState(null, '', '/desk/workspace?desk=' + cfg.desk.id);   // not yours, or gone: your own desk
    return openBuiltDesk(cfg);
  }
  if (W.pin) { W.pin = 0; history.replaceState(null, '', '/desk/workspace'); }
  W.phase = 'meet'; setPhaseAttr(); setPhase('brief');
  // a draft in progress in this tab survives a reload; ?new=1 starts a fresh one
  const fresh = q.has('new') && !q.has('resume');
  let sid = ''; try { sid = fresh ? '' : (localStorage.getItem('ws_sid') || ''); } catch (_) {}
  if (sid) {
    const s = await api('/design/' + encodeURIComponent(sid));
    if (s && !s.error && s.sid) {
      if (s.desk_id) { try { localStorage.removeItem('ws_sid'); } catch (_) {} location.replace('/desk/workspace?desk=' + s.desk_id); return; }
      return resumeDesign(s);
    }
  }
  if (!fresh) {                                                            // a draft started on another device or browser
    const s = await api('/design/mine');
    if (s && s.sid && !s.desk_id) { try { localStorage.setItem('ws_sid', s.sid); } catch (_) {} return resumeDesign(s, 'mine'); }
  }
  const s = await api('/design/start', {method: 'POST', body: {tier: W.tier}});
  if (!s || s.error) { addMsg('s', (s && s.error) || 'Atlas is not available right now.'); setState('unavailable'); renderAll(); return; }
  W.sid = s.sid; W.mode = s.mode || W.mode; W.tier = s.tier || W.tier; loadView();
  try { localStorage.setItem('ws_sid', s.sid); } catch (_) {}
  if (q.has('new')) history.replaceState(null, '', '/desk/workspace?new=1&resume=1');
  addMsg('a', (s.transcript && s.transcript[0] && s.transcript[0].text) || 'Describe the business and the work to take on.');
  addBriefs(s.suggestions || []);
  setDeskStatus(); loadMyDesks(); renderAll();
  if (W.liveReason) addMsg('s', 'no model key: ' + W.liveReason + ' (running the scripted designer)');
  tutStart(false, 'meet');
}
async function resumeDesign(s, from){
  W.sid = s.sid; W.mode = s.mode || W.mode; W.tier = s.tier || W.tier; loadView();
  (s.transcript || []).forEach(m => addMsg(m.role === 'user' ? 'u' : 'a', m.text || '', '', true));
  if ((s.transcript || []).length > 1) addMsg('s', from === 'mine' ? 'draft continued from your last session' : 'draft restored', 'assign');
  if ((s.transcript || []).some(m => m.role === 'user')) setSugg(s.suggestions || []); else addBriefs(s.suggestions || []);
  if (s.blueprint && (s.blueprint.agents || []).length) await applyBlueprint(s.blueprint, true);
  $('#build-btn').disabled = !(W.bp && (W.bp.agents || []).length);
  setDeskStatus(); loadMyDesks(); renderAll();
  tutStart(false, W.bp ? 'team' : 'meet');
}
async function openBuiltDesk(cfg){
  W.deskId = W.pin = cfg.desk.id; W.deskName = (cfg.business && cfg.business.name) || cfg.desk.business_name || cfg.desk.name;
  W.tier = cfg.desk.tier || 'free'; W.workflows = cfg.workflows || []; W.template = cfg.template || ''; W.cfgAgents = cfg.agents || [];
  loadView(); loadAgentsFromConfig(W.cfgAgents);
  W.phase = 'run'; setPhaseAttr(); setPhase('ready');            // straight into the console: no empty team, no transition
  setDeskStatus(); renderAll();
  layoutAll(false); spawnAll(true);
  const cams = await api('/cameras') || {};
  W.vlmReady = !!(cams.vlm && cams.vlm.ready);
  const list = (cams.cameras || []).map(c => ({name: c.name, id: c.id, source: (c.config || {}).source || '', journal: !!c.journal, alerts: !(c.rule && c.rule.alerts === false)}));
  if (list.length) await applyCams(list, true);
  ingestCams(cams);
  enterRunMode();
  loadMyDesks();
  if (W.cams.size) startCamPoll();
  setMode(liveCams() ? 'ask' : 'job');                           // asking the cameras is the default only while one is live
  W.greetEl = addMsg('a', greeting()); W.greetEl.classList.add('greet');   // kept current until the owner's first message
  loadLatestRun();
  tutStart(false, W.cams.size ? 'watching' : 'built');
}
// the desk's status in one line, the same facts and words as Home's sentence; the mode tabs under it say what a message does
function camSumText(){
  const cams = [...W.cams.values()], n = cams.length; if (!n) return '';
  const live = cams.filter(c => c.st === 'live').length, fix = cams.filter(c => c.problem).length, waiting = cams.filter(c => c.watching && c.st !== 'live').length;
  return `${live} of ${plural(n, 'camera')} live` + (fix ? `, ${plural(fix, 'feed')} to fix` : waiting ? `, ${waiting} waiting for frames` : live < n ? ', not being watched' : '');
}
const liveCams = () => [...W.cams.values()].filter(c => c.st === 'live').length;
// Atlas's opening status: the desk in one line, then what needs the owner and the last job (the same facts as the header)
function greeting(){
  const out = [`**${W.deskName}**`];
  if (W.apCount != null) out.push('- Approvals: ' + (W.apCount ? `${W.apCount} waiting, oldest ${W.apOldest}` : 'none waiting'));
  if (W.cams.size) out.push('- Cameras: ' + camSumText());
  const g = gapCount(); if (g) out.push(`- Gaps: ${plural(g, 'agent gap')} to fix, under Explorer › Attention`);
  if (W.runs) { const r = W.runs[0], t = r ? String(r.task || '').replace(/\s+/g, ' ') : ''; out.push('- Last job: ' + (r ? `${runState(r)[0]} ${fmtTs(r.created)} UTC · ${t.length > 48 ? t.slice(0, 47) + '…' : t}` : 'none yet, give the team its first one below')); }
  return out.join('\n');
}
function refreshGreeting(){ const el = W.greetEl; if (!el || !el.isConnected || W.greetDone) return; const h = md(greeting()); if (el.innerHTML !== h) { el.innerHTML = h; if (W.msgsPinned) $('#msgs').scrollTop = 1e9; } }

function loadAgentsFromConfig(list){
  W.agents.clear(); W.order = [];
  list.forEach((a, i) => { if (a.id === 'atlas') return;
    W.agents.set(a.id, {id: a.id, name: a.name, role: a.role || '', goal: a.goal || '', instructions: a.instructions || [], tools: (a.tools || []).filter(t => !['delegate', 'list_agents', 'finish'].includes(t)),
      engine: a.engine || 'atlas', engine_note: a.engine_note || '', reports_to: a.reports_to || 'atlas', members: a.members || [], color: a.color || PALETTE[i % PALETTE.length]}); });
  W.order = orderIds();
}
function atlasCfg(){ return (W.cfgAgents || []).find(a => a.id === 'atlas') || null; }
function engineLabel(a){
  if (W.deskId && W.mode === 'demo') return {t: 'demo script', title: 'Demo mode: agents follow a script; no model calls'};
  if (a.engine_note) return {t: 'built-in', title: a.engine_note};
  return {t: a.engine === 'hermes_agent' ? 'Hermes Agent' : 'built-in', title: W.deskId ? '' : 'configured; takes effect when the desk is built'};
}

/* ------------------------------------------------------------------ chat */
function addMsg(role, text, cls, noTime){
  const d = document.createElement('div'); d.className = 'm ' + role + (cls ? ' ' + cls : '');
  d.dataset.who = role === 'u' ? 'You' : (role === 'a' ? 'Atlas' : ''); d.dataset.t = noTime ? '' : hhmmss() + ' UTC';
  if (role === 'a') { d.classList.add('md'); d.innerHTML = md(text); } else d.textContent = text;
  $('#msgs').appendChild(d); $('#msgs').scrollTop = 1e9; return d;
}
function setSugg(list){
  W.suggFns = [];
  $('#sugg').innerHTML = (list || []).filter(Boolean).map((s, i) => { if (typeof s === 'object') { W.suggFns[i] = s.run; return `<button onclick="suggRun(${i})">${esc(s.label)}</button>`; }
    return `<button onclick="send(${jsq(s)})">${esc(s)}</button>`; }).join('');
}
// a brand-new desk: the ways to start, as cards in the thread (each one sends that brief to Atlas)
function addBriefs(list){
  const old = $('#briefs'); if (old) old.remove();
  const items = (list || []).filter(t => typeof t === 'string' && t.trim()); setSugg([]);
  if (!items.length) return;
  const d = document.createElement('div'); d.id = 'briefs'; d.className = 'briefs';
  d.innerHTML = `<div class="gk">Start from a brief</div>` + items.map(t => `<button class="brief" onclick="send(${jsq(t)})"><svg class="i"><use href="#${/camera|footage|cctv/i.test(t) ? 'i-cam' : 'i-send'}"/></svg><span>${esc(t)}</span><svg class="i i14 go"><use href="#i-chev"/></svg></button>`).join('');
  $('#msgs').appendChild(d); $('#atlas-panel').classList.add('briefing');
}
function dropBriefs(){ const b = $('#briefs'); if (b) b.remove(); $('#atlas-panel').classList.remove('briefing'); }
function suggRun(i){ const f = (W.suggFns || [])[i]; if (f) f(); }
function sayKey(ev){ if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(); } }
const CAM_QUESTIONS = ['What happened in the last 10 minutes?', 'Was anything left behind?', 'Who waited the longest?', 'Describe everyone who came in'];
const MODE_PH = {ask: 'Ask about what the cameras recorded', job: 'Describe a job for the team: it starts a run', change: 'Describe a change to the team: Atlas proposes it on the Team canvas'};
function modeSugg(m){
  if (m === 'ask') return CAM_QUESTIONS;
  if (m === 'job') return W.cams.size ? ['Summarise what the cameras saw today', 'Write a safety report for the last 24 hours'] : ['Draft a reply to the newest enquiry', "Prepare tomorrow's staff briefing"];
  return ['Add a reviewer who checks every report before it goes out', 'Email me a report of the day every evening'];
}
// a built desk: the owner says what the message is for (ask the cameras, give a job, change the team); never guessed
function setMode(m){
  if (m === 'design') m = 'change';
  if (m === 'ask' && !W.cams.size) m = 'job';
  W.modeSel = m;
  document.querySelectorAll('#as-modes button').forEach(b => { b.classList.toggle('on', b.dataset.m === m); if (b.dataset.m === 'ask') b.classList.toggle('hide', !W.cams.size); });
  $('#say').placeholder = MODE_PH[m] || MODE_PH.job;
  setSugg(modeSugg(m));
}
function looksLikeQuestion(t){ return /\?\s*$/.test(t) || /^(who|what|when|where|why|how|was|were|did|does|do|is|are|has|have|had|can|could|any|anyone|anything)\b/i.test(t.trim()); }

async function send(text){
  text = (text || $('#say').value).trim(); if (!text) return;
  W.greetDone = true;
  if (W.busy) return toast('Atlas is still answering: your message stays in the box');
  $('#say').value = '';
  if (/^switch to the paid model/i.test(text) && W.sid && !W.deskId) { await setTier('balanced'); return send(W.lastSaid || ''); }
  if (W.phase === 'run' || W.phase === 'load') {
    if (W.phase === 'load') return toast('The desk is still loading');
    const m = W.modeSel || 'job';
    if (m === 'ask') return askCams(text);
    if (m === 'change') return changeTeam(text);
    if (looksLikeQuestion(text)) {                     // a question never starts a run by accident
      addMsg('u', text);
      addMsg('s', 'That reads like a question. In "Give a job" every message starts a run of the team.');
      setSugg([{label: 'Run it as a job', run: () => deploy(text, null, true)}, W.cams.size ? {label: 'Ask the cameras instead', run: () => { setMode('ask'); askCams(text, true); }} : null]);
      return;
    }
    $('#job').value = text; return deploy();
  }
  W.lastSaid = text;
  dropBriefs(); addMsg('u', text); setSugg([]);
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
  $('#build-btn').title = W.bp ? 'Build the desk from this draft' : 'Available once Atlas has drafted a team';
  $('#build-hint').textContent = result.ready ? 'Draft complete. Build it, or keep refining.' : (W.bp ? 'Describe changes to reshape the team, or build now.' : 'Build is available once there is a first draft.');
  if (W.bp) $('#chat-foot').classList.remove('hide');
  if (result.ready) tutHook('ready');
}
// a built desk's team is changed on Home's team canvas, where Atlas proposes it and nothing changes until applied
async function changeTeam(text){
  addMsg('u', text);
  addMsg('a', 'Opening the **team canvas** on Home: Atlas proposes this change there, and nothing changes until you apply it.');
  await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});      // an explicit hand-off: Home opens on this desk
  setTimeout(() => { if (window.shell && shell.go) shell.go('/desk#team', 'teamDesignFor:' + encodeURIComponent(text)); else location.href = '/desk#team'; }, 700);
}

/* ------------------------------------------------------------------ workspace open + team diff/animation */
function openWorkspace(){
  if (W.phase !== 'meet') return;
  W.phase = 'design'; setPhaseAttr(); setPhase('draft');
  if (!$('#ws').classList.contains('dock')) openAtlas();          // one column: the conversation stays in view
  renderAll(); layoutAll(false);
}
function orderIds(){
  const tops = [...W.agents.values()].filter(a => a.reports_to === 'atlas' || !W.agents.has(a.reports_to));
  const out = [];
  tops.forEach(t => { out.push(t.id); (t.members || []).forEach(m => { if (W.agents.has(m)) out.push(m); }); });
  W.agents.forEach(a => { if (!out.includes(a.id)) out.push(a.id); });
  return out;
}
async function applyBlueprint(bp, quiet){
  W.bp = bp;
  // positions the owner dragged to, saved with the blueprint: the canvas never snaps back after a reply (local wins)
  Object.entries(bp.layout || {}).forEach(([id, p]) => { if (!W.manual.has(id) && p && typeof p.x === 'number') W.manual.set(id, {x: p.x, y: p.y}); });
  const incoming = new Map();
  (bp.agents || []).forEach((a, i) => incoming.set(a.id, {id: a.id, name: a.name, role: a.role || '', goal: a.goal || '', instructions: a.instructions || [],
    tools: a.tools || [], engine: a.engine || 'atlas', engine_note: '', reports_to: a.reports_to || 'atlas', members: a.members || [], color: a.color || PALETTE[i % PALETTE.length]}));
  const business = bp.business || {};
  if (business.name) W.deskName = business.name;
  const first = W.phase === 'meet';
  if (first) openWorkspace();
  const added = [], removed = [], moved = [], changed = [];
  incoming.forEach((a, id) => { const old = W.agents.get(id); if (!old) added.push(a); else { if (old.reports_to !== a.reports_to) moved.push(a); else if (old.role !== a.role || old.name !== a.name || (old.tools || []).join() !== (a.tools || []).join()) changed.push(a); } });
  W.agents.forEach((a, id) => { if (!incoming.has(id)) removed.push(a); });
  if (first && added.length && !quiet) addMsg('s', 'drafting the team', 'assign');
  W.agents = incoming; W.order = orderIds();
  removed.forEach(a => { const el = nodeEl(a.id); if (el) { el.classList.add('gone'); setTimeout(() => el.remove(), 500); } removeEdge(a.id); if (!quiet) addMsg('s', `− ${a.name} removed`, 'assign'); });
  layoutAll(!quiet, added.map(a => a.id));
  for (const a of added) {
    const lead = a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? W.agents.get(a.reports_to) : null;
    if (!quiet) addMsg('s', `+ ${a.name} — ${a.role}${lead ? `  (in ${lead.name}'s team)` : ''}`, 'assign');
    await spawn(a.id, lead ? lead.id : 'atlas', quiet);
    if (!quiet) { renderTeamTable(); await sleep(160); }
  }
  moved.forEach(a => { const to = a.reports_to === 'atlas' ? 'Atlas' : (W.agents.get(a.reports_to) || {}).name || a.reports_to; if (!quiet) addMsg('s', `↳ ${a.name} now reports to ${to}`, 'assign'); removeEdge(a.id); drawEdge(a.id, !quiet); });
  changed.forEach(a => { const el = nodeEl(a.id); if (el) { renderNodeInner(el, a); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); } });
  W.addedCams.forEach((src, name) => { const list = bp.cameras = bp.cameras || []; const c = list.find(x => x.name === name); if (c) { if (!c.source) c.source = src; } else list.push({name, source: src, journal: true, notes: '', focus: ''}); });   // hand-attached feeds survive a redraft
  await applyCams(bp.cameras || [], quiet);
  if (first && !quiet) tutHook('team');
  renderAll();
  if (W.sel && W.sel !== 'atlas' && !W.agents.has(W.sel)) closeInsp();
  else if (W.sel) inspect(W.sel);
}
async function spawnAll(instant){
  layoutAll(false);
  for (const id of W.order) { const a = W.agents.get(id); const lead = a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? a.reports_to : 'atlas'; await spawn(id, lead, instant); if (!instant) await sleep(140); }
  if (instant) redrawEdges();
}

/* ---- layout: Atlas col 0, top-level col 1, members col 2; vertical stack centred (top-aligned when taller than the canvas) */
const NW = 236, NW_ATLAS = 214, COL = 300, ROW_MAX = 118, GAP_MAX = 26, K_MIN = .8;
function layoutAll(animate, freshIds){
  const c = $('#canvas'); const H0 = c.clientHeight || 600, W0 = c.clientWidth || 900;
  const tops = W.order.filter(id => { const a = W.agents.get(id); return a && (a.reports_to === 'atlas' || !W.agents.has(a.reports_to)); });
  const blocks = tops.map(id => ({id, members: (W.agents.get(id).members || []).filter(m => W.agents.has(m))}));
  const tallest = [...document.querySelectorAll('#nodes .node:not(.atlas):not(.ghost)')].reduce((m, n) => Math.max(m, n.offsetHeight || 0), 0);
  const ROW = Math.max(ROW_MAX, tallest + 14), GAP = GAP_MAX;               // never overlap: a row is as tall as the tallest card
  const heights = blocks.map(b => Math.max(1, b.members.length) * ROW);
  const total = heights.reduce((s, h) => s + h, 0) + Math.max(0, blocks.length - 1) * GAP;
  const hasMembers = blocks.some(b => b.members.length);
  const needW = NW_ATLAS + COL + (hasMembers ? COL : 0) + (NW - 30) + 48;
  const k = W.k = Math.max(K_MIN, Math.min(1, (H0 - 40) / Math.max(1, total), W0 / needW)) * (W.uz || 1);   // never below a readable size: pan instead
  const H = Math.max(H0 / k, total + 40), Wd = Math.max(W0 / k, needW);
  W.vb = [Wd, H];                                                    // the edges' viewBox must match the box the nodes were laid out in
  ['#nodes', '#edges'].forEach(q => { const st = $(q).style; st.inset = 'auto'; st.left = st.top = '0'; st.width = Wd + 'px'; st.height = H + 'px'; st.transformOrigin = '0 0'; }); applyView();
  const x0 = Math.max(24, Math.round((Math.min(Wd, W0 / k) - (NW_ATLAS + COL + (hasMembers ? COL : 0) + (NW - 30))) / 2));
  const fits = total <= H0 / k - 40;
  let y = fits ? Math.max(20, Math.round((H0 / k - total) / 2)) : 20;     // taller than the view: start at the top, pan for the rest
  const atlasY = fits ? Math.max(20, Math.round(y + total / 2 - 48)) : 20;
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
function nodeEl(id){ return document.getElementById('n-' + String(id).replace(/[^a-zA-Z0-9_#-]/g, '-')); }
function ensureAtlasNode(){
  let el = nodeEl('atlas'); const p = W.pos.get('atlas');
  if (!el) { el = document.createElement('div'); el.className = 'node atlas'; el.id = 'n-atlas'; el.dataset.id = 'atlas'; el.onclick = () => { if (!W.dragged) inspect('atlas'); };
    el.innerHTML = `<div class="nm">Atlas <span class="tag lead">lead</span></div><div class="role">Orchestrator · briefs, reviews, approves</div><div class="asg"></div><div class="chips"></div><div class="out"></div><div class="ft"></div>`;
    el.style.transition = 'none'; el.style.transform = `translate(${p.x}px,${p.y}px)`; $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = ''; }
  else el.style.transform = `translate(${p.x}px,${p.y}px)`;
  const ac = atlasCfg(); if (ac && !W.run) el.querySelector('.chips').innerHTML = (ac.tools || []).filter(t => !['list_agents', 'finish'].includes(t)).slice(0, 4).map(t => `<span>${esc(t)}</span>`).join('');
}
function renderNodeInner(el, a){
  const isLead = (a.members || []).length > 0; const member = a.reports_to && a.reports_to !== 'atlas' && W.agents.has(a.reports_to);
  el.style.setProperty('--c', a.color);
  el.querySelector('.nm').innerHTML = `${esc(a.name)} <span class="tag ${isLead ? 'lead' : ''}">${isLead ? 'sub-team lead' : (member ? '↳ ' + esc((W.agents.get(a.reports_to) || {}).name || a.reports_to) : (W.phase === 'run' ? 'ready' : 'drafted'))}</span>`;
  el.querySelector('.role').textContent = a.role;
  if (!el.querySelector('.chips').children.length || !W.run) el.querySelector('.chips').innerHTML = (a.tools || []).slice(0, 5).map(t => `<span>${esc(t)}</span>`).join('');
}
async function spawn(id, fromId, instant){
  const a = W.agents.get(id); if (!a) return; if (nodeEl(id)) { renderNodeInner(nodeEl(id), a); return; }
  const to = W.pos.get(id) || W.pos.get(fromId) || W.pos.get('atlas') || {x: 40, y: 40, w: 200};
  const el = document.createElement('div'); el.className = 'node' + (instant ? '' : ' spawn'); el.id = 'n-' + id; el.dataset.id = id; el.onclick = () => { if (!W.dragged) inspect(id); };
  el.innerHTML = `<div class="nm"></div><div class="role"></div><div class="asg"></div><div class="chips"></div><div class="out"></div><div class="ft"></div>`;
  renderNodeInner(el, a);
  el.style.transition = 'none'; el.style.transform = `translate(${to.x}px,${to.y}px)`;      // fades in place, no fly-out
  $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = '';
  if (instant) { drawEdge(id, false); return; }
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
// the conversation stays pinned to its newest line while the panel resizes, unless the owner scrolled up to read
(function pinMsgs(){ const m = document.getElementById('msgs'); W.msgsPinned = true;
  const user = () => { W.msgUserT = Date.now(); };                       // only the owner's own scrolling unpins it
  ['wheel', 'touchmove', 'keydown', 'pointerdown'].forEach(t => m.addEventListener(t, user, {passive: true}));
  m.addEventListener('scroll', () => { const bottom = m.scrollTop + m.clientHeight >= m.scrollHeight - 12; if (bottom) W.msgsPinned = true; else if (Date.now() - (W.msgUserT || 0) < 600) W.msgsPinned = false; });
  if (window.ResizeObserver) new ResizeObserver(() => { if (W.msgsPinned) m.scrollTop = 1e9; }).observe(m); })();
function msgsBottom(){ W.msgsPinned = true; requestAnimationFrame(() => { $('#msgs').scrollTop = 1e9; }); }
// re-lay out whenever the canvas changes size (window, rail collapse, the column opening, the tab changing)
if (window.ResizeObserver) new ResizeObserver(() => { if (W.phase === 'meet') return; cancelAnimationFrame(W.relayout); W.relayout = requestAnimationFrame(() => layoutAll(false)); }).observe(document.getElementById('canvas'));

/* ------------------------------------------------------------------ inspector */
function inspect(id){
  W.sel = id; document.querySelectorAll('.node.sel').forEach(n => n.classList.remove('sel')); const el = nodeEl(id); if (el) el.classList.add('sel');
  const ac = atlasCfg();
  const a = id === 'atlas'
    ? {name: 'Atlas', role: (ac && ac.role) || 'Orchestrator', goal: (ac && ac.goal) || 'Briefs the team, runs leads in parallel, reviews every result, and holds anything outbound for your approval.',
       instructions: (ac && ac.instructions) || [], tools: ac ? (ac.tools || []).filter(t => !['list_agents', 'finish'].includes(t)) : [], engine: (ac && ac.engine) || 'atlas', engine_note: (ac && ac.engine_note) || '', reports_to: ''}
    : W.agents.get(id);
  if (!a) return;
  $('#insp-k').textContent = id === 'atlas' ? 'Lead' : 'Agent'; $('#insp-name').textContent = a.name; $('#insp-sub').textContent = a.role || '';
  const st = W.run && [...W.run.inst.values()].filter(s => s.base === id);
  const out = st && st.length ? st.map(s => `<h4>${esc(s.inst)} · ${esc(s.status)}</h4>${s.assignment ? `<div style="margin-bottom:5px">${esc(s.assignment)}</div>` : ''}<pre id="live-${esc(s.inst)}">${esc(s.text || (W.run.active && !['done', 'error'].includes(s.status) ? '(waiting for output…)' : '(no output recorded)'))}</pre>`).join('') : '';
  const ask = id === 'atlas' ? [] : [`Give ${a.name} web access`, `Make ${a.name} lead a pod of 3`, `Merge ${a.name} into another role`, `Remove ${a.name}`, `Rewrite ${a.name}'s standing orders to be stricter`];
  const eng = engineLabel(a);
  const lead = a.reports_to === 'atlas' || !a.reports_to ? (id === 'atlas' ? 'you, the owner' : 'Atlas') : ((W.agents.get(a.reports_to) || {}).name || a.reports_to);
  const gaps = agentGaps(id);
  // one section per fact (gaps, live output, goal, orders, tools, details, changes): stacked in a drawer, side by side under the table
  $('#insp-body').innerHTML = `
    ${gaps.length ? `<section class="is wide igap"><h4>Cannot do part of its job</h4><ul>${gaps.map(x => `<li>${esc(x)}</li>`).join('')}</ul></section>` : ''}
    ${out ? `<section class="is wide">${out}</section>` : ''}
    ${a.goal ? `<section class="is"><h4>Goal</h4><div>${esc(a.goal)}</div></section>` : ''}
    ${(a.instructions || []).length ? `<section class="is"><h4>Standing orders</h4><ul>${a.instructions.map(x => `<li>${esc(x)}</li>`).join('')}</ul></section>` : ''}
    <section class="is"><h4>Tools · ${(a.tools || []).length}</h4><div class="chips">${(a.tools || []).map(t => `<span>${esc(t)}</span>`).join('') || `<span>${W.deskId ? 'none' : 'set when the desk is built'}</span>`}</div></section>
    <section class="is"><h4>Details</h4><div class="kv"><span>Engine</span><span title="${esc(eng.title)}">${esc(eng.t)}</span><span>Reports to</span><span>${esc(lead)}</span>${(a.members || []).length ? `<span>Leads</span><span>${a.members.map(m => esc((W.agents.get(m) || {}).name || m)).join(', ')}</span>` : ''}</div></section>
    ${ask.length ? `<section class="is"><h4>${W.phase === 'design' ? 'Ask Atlas to…' : 'Change with Atlas'}</h4><div class="ask">${ask.map(s => `<button onclick="inspAsk(${jsq(s)})">${esc(s)}</button>`).join('')}</div></section>` : ''}`;
  $('#insp').classList.add('on');
  document.querySelectorAll('.team tr.sel').forEach(r => r.classList.remove('sel')); const tr = document.getElementById('tr-' + id); if (tr) tr.classList.add('sel');
  renderExplorer();
}
// the team panel's own actions on a built desk: a new specialist is added on Home's team canvas (this desk), a change goes through Atlas
async function addAgent(){ if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}}); if (window.shell && shell.go) shell.go('/desk#team', 'teamAddReady'); else location.href = '/desk#team'; }
function changeTeamPrompt(){ setMode('change'); openAtlas(); }
function inspAsk(s){ closeInsp(); if (W.phase === 'design') return send(s); setMode('change'); $('#say').value = s; openAtlas(); }
// the Team tab is a list and its detail: the table (or org chart) with the selected agent's brief beside it. In the
// Overview, a small team's brief sits under its rows when the panel has the room (fitTeam). Elsewhere it is a drawer.
function placeInsp(){
  const insp = $('#insp'), tp = $('#team-panel');
  const mode = W.tab === 'team' && !isPhone() && W.phase !== 'meet' ? 'inline' : (W.below && W.tab !== 'team' && W.teamView === 'table' ? 'below' : '');
  tp.classList.toggle('has-below', mode === 'below');
  if (!mode) { if (insp.classList.contains('inline')) { insp.classList.remove('inline', 'below'); $('#ws').appendChild(insp); closeInsp(); } return; }
  if (mode === 'inline' && insp.parentElement !== $('#pane-team')) $('#pane-team').appendChild(insp);
  if (mode === 'below' && insp.parentElement !== tp) tp.insertBefore(insp, $('#team-foot'));
  insp.classList.add('inline'); insp.classList.toggle('below', mode === 'below');
  const id = W.sel && (W.sel === 'atlas' || W.agents.has(W.sel)) ? W.sel : (W.order[0] || 'atlas');
  inspect(id);
}
function closeInsp(){
  if ($('#insp').classList.contains('inline')) { const id = W.sel === 'atlas' || W.agents.has(W.sel) ? W.sel : (W.order[0] || 'atlas'); if (id !== W.sel || !W.sel) inspect(id); return; }   // inline: always shows one agent
  W.sel = null; $('#insp').classList.remove('on'); document.querySelectorAll('.node.sel, .team tr.sel').forEach(n => n.classList.remove('sel')); renderExplorer(); }

/* ------------------------------------------------------------------ build */
// a draft that names no business: the owner names the desk before it is built, so no desk is ever called "New desk"
async function askDeskName(){
  if (!W.me) W.me = await api('/me') || {};
  const inp = $('#nm-in'); if (!inp.value) inp.value = String(W.me.company || '').trim();
  $('#namedlg').classList.remove('hide'); setTimeout(() => { inp.focus(); inp.select(); }, 50);
}
function closeDeskName(){ $('#namedlg').classList.add('hide'); }
function nameAndBuild(){
  const v = $('#nm-in').value.trim().replace(/\s+/g, ' ').slice(0, 60); if (!v) { $('#nm-in').focus(); return; }
  W.deskName = v; closeDeskName(); renderHead(); buildDesk();
}
async function buildDesk(){
  if (!W.bp || !(W.bp.agents || []).length) return;
  if (!W.deskName) return askDeskName();
  $('#build-btn').disabled = true; $('#build-hint').textContent = 'Building…'; setState('building');
  const r = await api(`/design/${W.sid}/build`, {method: 'POST', body: {blueprint: W.bp, tier: W.tier, name: W.deskName || ''}});
  setState('ready');
  if (!r || r.error) { $('#build-hint').textContent = (r && r.error) || 'build failed'; $('#build-btn').disabled = false; return; }
  W.deskId = W.pin = r.desk.id; W.deskName = r.desk.business_name || r.desk.name; saveView();
  history.replaceState(null, '', '/desk/workspace?desk=' + W.deskId);      // a reload now opens this desk, not a new conversation
  try { localStorage.removeItem('ws_sid'); } catch (_) {}
  const cfg = await api('/config'); if (cfg && cfg.desk && cfg.desk.id === W.deskId) { W.cfgAgents = cfg.agents || []; W.workflows = cfg.workflows || []; W.template = cfg.template || ''; W.tier = cfg.desk.tier || W.tier;
    const byId = new Map(W.cfgAgents.map(a => [a.id, a])); W.agents.forEach(a => { const c = byId.get(a.id); if (c) { a.engine = c.engine || a.engine; a.engine_note = c.engine_note || ''; a.tools = (c.tools || a.tools).filter(t => !['delegate', 'list_agents', 'finish'].includes(t)); } }); }
  (r.cameras || []).forEach(c => { const k = W.cams.get(c.name); if (k) { k.id = c.id; k.journal = c.journal; k.st = 'waiting'; k.watching = true; renderCamTile(k); } });
  const camLine = (r.cameras || []).length ? ` ${plural(r.cameras.length, 'camera')} starting: each writes its journal once its frames arrive.` : '';
  const missing = (r.cameras_missing || []).length ? ` Still needed: the stream address for ${r.cameras_missing.join(', ')} (click the camera tile to add it).` : '';
  addMsg('a', `Desk built: Atlas + ${plural(W.agents.size, 'specialist')}. Outbound messages wait for your approval.${camLine}${missing} Press **New job** (N) to give the team its first job.`);
  toast('Desk built');
  if (window.shell && shell.remount) shell.remount();                    // the setup rail becomes the full one
  enterRunMode();
  setMode('job');
  if ((r.cameras || []).length) { startCamPoll(); tutHook('watching'); } else tutHook('built');
}
function enterRunMode(){
  W.phase = 'run'; setPhaseAttr(); setPhase('ready');
  $('#chat-foot').classList.add('hide'); $('#build-btn').classList.add('hide'); $('#newjob-btn').classList.remove('hide');
  $('#as-modes').classList.remove('hide');
  $('#job').placeholder = W.cams.size ? 'Give the team a job, e.g. a safety summary of the last 24 hours' : 'Give the team a job, e.g. a reply to an enquiry, a report, a list to chase';
  document.querySelectorAll('.node .nm .tag').forEach(t => { if (!t.classList.contains('lead') && !t.textContent.startsWith('↳')) t.textContent = 'ready'; });
  setDeskStatus(); renderWorkflows(); renderRunPanel();
  loadApprovals(); loadRuns(); pollHealth(); pollStats(); loadGaps(); renderAll();
  clearInterval(W.statsT); W.statsT = setInterval(() => { if (!document.hidden) pollStats(); }, 15000);
}

/* ------------------------------------------------------------------ run: real events, live output */
async function deploy(task, mode, echoed){
  task = (typeof task === 'string' ? task : $('#job').value).trim();
  if (!task) { showTab('runs'); $('#job').focus(); return toast('Type the job first'); }
  if (W.run && W.run.active) return toast('A run is already going: wait for it to finish');
  const r = await api('/runs', {method: 'POST', body: {task, mode: mode || 'auto'}});
  if (!r || r.error) return toast((r && (r.message || r.error)) || 'could not start');
  $('#job').value = '';
  if (W.phase === 'run') setSugg(modeSugg(W.modeSel || 'job'));          // offers made for an earlier message no longer apply
  if (!echoed) addMsg('u', task);
  addMsg('s', `run ${r.run_id} started`, 'assign');
  $('#summary').classList.add('hide'); $('#summary-empty').classList.remove('hide'); $('#feed').innerHTML = '';
  document.querySelectorAll('.node.ghost').forEach(n => n.remove()); $('#edges').querySelectorAll('[data-e*="#"]').forEach(p => p.remove());
  resetNodes();
  W.run = {id: r.run_id, inst: new Map(), active: true, tin: 0, tout: 0, task, started: Date.now(), status: ''};
  setPhase('running'); setState('running');
  $('#run-cancel').classList.remove('hide'); $('#tab-n-runs').classList.remove('hide');
  renderRunPanel(); renderAll(); renderRunPill(); showTab('overview');
  attachStream(r.run_id, 0);
  shellRefresh();
  tutHook('running');
}
function resetNodes(){
  document.querySelectorAll('.node').forEach(n => { n.classList.remove('busy', 'done', 'error'); n.querySelector('.out').textContent = ''; n.querySelector('.asg').textContent = ''; n.querySelector('.ft').textContent = ''; const t = n.querySelector('.nm .tag'); if (t && !t.classList.contains('lead') && !t.textContent.startsWith('↳')) { t.textContent = 'ready'; t.className = 'tag'; } });
  W.agents.forEach(a => { const el = nodeEl(a.id); if (el) el.querySelector('.chips').innerHTML = ''; });
  const atlas = nodeEl('atlas'); if (atlas) atlas.querySelector('.chips').innerHTML = '';
}
function attachStream(id, since){
  if (W.run && W.run.es) W.run.es.close();
  const es = new EventSource(withDesk(`/api/stream?run=${encodeURIComponent(id)}&since=${since === 'now' ? 'now' : 0}`));
  W.run.es = es;
  es.onmessage = m => { try { onRunEvent(JSON.parse(m.data)); } catch (_) {} };
  es.onerror = () => { if (W.run && !W.run.active) es.close(); };
}
function instState(e){
  const inst = (e.data && e.data.inst) || e.agent; const base = String(inst).split('#')[0];
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
const KIND = {agent_start: 'start', agent_end: 'done', tool: 'tool', approval: 'approval', policy: 'policy', error: 'error', done: 'finished', deliverable: 'saved', journal: 'note', digest: 'summary', summary: 'summary', log: 'log'};
function feed(e){
  const f = $('#feed'); const d = document.createElement('div');
  let kind = KIND[e.kind] || String(e.kind || 'event');
  let text = String(e.text || '');
  if (e.kind === 'done' && (/error|fail|cancel/.test((e.data && e.data.status) || '') || /^ERROR from /.test(text))) kind = 'failed';
  if (e.kind === 'deliverable') text = String((e.data && e.data.path) || text).split(/[\\/]/).pop();
  if (e.kind === 'done') text = text.split('---')[0].trim().split('\n')[0] || (kind === 'failed' ? 'run failed' : 'run finished');
  const ts = e.ts || Date.now() / 1000; const today = sameUtcDay(new Date(ts * 1000), new Date());
  d.className = 'k-' + kind; d.title = `${fullTs(ts)} · ${agentName((e.data && e.data.inst) || e.agent)} · ${e.kind}\n${String(e.text || '').slice(0, 600)}`;
  d.innerHTML = `<span class="t">${today ? hhmmss(new Date(ts * 1000)) : esc(fmtTs(ts).split(' ').slice(0, 2).join(' '))}</span><span class="k">${esc(kind)}</span><span class="x"><span class="a">${esc(agentName((e.data && e.data.inst) || e.agent))}</span>${esc(text)}</span>`;
  f.appendChild(d); f.scrollTop = 1e9; while (f.children.length > 150) f.firstChild.remove();
}
function runFailed(status, summary){ return /error|fail|cancel/.test(status || '') || /^ERROR from /.test(summary || ''); }
function onRunEvent(e){
  if (!W.run || e.run_id !== W.run.id) return;
  if (typeof e.data === 'string') { try { e.data = JSON.parse(e.data || '{}'); } catch (_) { e.data = {}; } }
  e.data = e.data || {};
  const live = !W.replay;
  if (e.kind === 'usage') { W.run.tin = e.data.tokens_in; W.run.tout = e.data.tokens_out; renderRunMeta(); return; }
  if (e.kind === 'token') { if (e.data.thinking) return; const s = instState(e); s.text += e.text; if (s.text.length > 6000) s.text = s.text.slice(-6000); const el = elFor(s); if (el) { const o = el.querySelector('.out'); o.textContent = s.text.slice(-420); } const lv = document.getElementById('live-' + s.inst); if (lv) { lv.textContent = s.text; lv.scrollTop = 1e9; } if (s.status !== 'writing') { s.status = 'writing'; setTag(el, 'writing', 'busy'); renderRunPanel(); renderTeamTable(); chartSoon(); } else renderRunLive(s); return; }
  if (e.agent !== 'system' && e.agent !== 'owner') {
    const s = instState(e); const el = elFor(s);
    if (e.kind === 'agent_start') { s.status = 'thinking'; s.assignment = e.data.assignment || ''; s.text = ''; if (el) { el.classList.add('busy'); el.classList.remove('done', 'error'); el.querySelector('.asg').textContent = s.assignment; el.querySelector('.out').textContent = ''; setTag(el, 'thinking', 'busy'); } edgeState(s.inst === 'atlas' ? null : (s.ghost ? s.inst : s.base), 'on'); if (live && s.base !== 'atlas' && s.assignment) addMsg('s', `→ ${agentName(s.base)}: ${s.assignment.slice(0, 90)}`, 'assign'); }
    else if (e.kind === 'log') { s.turns++; s.status = 'thinking'; const tx = String(e.text || ''); if (tx && !tx.startsWith('(')) { s.text = tx.slice(-6000); if (el) el.querySelector('.out').textContent = s.text.slice(-420); } if (el) { setTag(el, `turn ${s.turns}`, 'busy'); el.querySelector('.ft').textContent = plural(s.turns, 'turn'); } }
    else if (e.kind === 'tool') { const name = String(e.text || '').split(/[( →:]/)[0]; s.chips.push(name); if (el) { const c = el.querySelector('.chips'); c.querySelectorAll('.hot').forEach(x => x.classList.remove('hot')); const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = name; c.appendChild(sp); while (c.children.length > 7) c.firstChild.remove(); setTag(el, name, 'busy'); } if (name === 'delegate') { const m = String(e.text).match(/delegate → ([\w#-]+)/); if (m) edgeState(m[1], 'on'); } }
    else if (e.kind === 'agent_end') { s.status = 'done'; if (el) { el.classList.remove('busy'); el.classList.add('done'); setTag(el, 'done', 'done'); } edgeState(s.ghost ? s.inst : s.base, 'done'); }
    else if (e.kind === 'error') { s.status = 'error'; s.err = String(e.text || '').slice(0, 300); if (el) { el.classList.remove('busy'); el.classList.add('error'); setTag(el, 'error', ''); } }
    else if (e.kind === 'approval') { if (el) { const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = 'waiting: ' + (e.data.action_kind || 'approval'); el.querySelector('.chips').appendChild(sp); } if (live) { loadApprovals(); shellRefresh(); toast('Waiting for your approval: ' + (e.data.action_kind || 'an outbound action')); tutHook('approval'); } }
    else if (e.kind === 'policy') { if (el) { const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = 'policy'; el.querySelector('.chips').appendChild(sp); } }
    if (live && W.sel === s.base && ['agent_start', 'agent_end', 'tool', 'log'].includes(e.kind)) inspect(s.base);
    if (live && ['agent_start', 'agent_end', 'tool', 'log', 'error', 'approval'].includes(e.kind)) { renderRunPanel(); renderTeamTable(); renderExplorer(); renderKpis(); headSoon(); chartSoon(); }
  }
  if (e.kind === 'done') {
    const text = String(e.text || '');
    W.run.active = false; W.run.ended = live ? Date.now() : (e.ts || 0) * 1000 || W.run.ended; W.run.summary = text;
    W.run.status = runFailed(e.data.status || W.run.status, text) ? (e.data.status && e.data.status !== 'done' ? e.data.status : 'error') : (e.data.status || 'done');
    if (W.run.es) W.run.es.close();
    $('#run-cancel').classList.add('hide'); $('#tab-n-runs').classList.add('hide');
    document.querySelectorAll('.node.busy').forEach(n => { n.classList.remove('busy'); n.classList.add('done'); setTag(n, 'done', 'done'); });
    $('#edges').querySelectorAll('path.on').forEach(p => { p.classList.remove('on'); p.classList.add('done'); });
    setPhase('ready'); setState('ready');
    showSummary(W.run.id, W.run.status, text);
    if (live) {
      const failed = runFailed(W.run.status, text);
      addMsg('a', failed ? `**Run failed.** ${text.split('\n\n---\n')[0].replace(/^ERROR from [^:]+:\s*/, '').slice(0, 300)}` : `Run finished. ${text.split('---').pop().trim()}`.trim(), failed ? 'err' : '');
      loadApprovals(); loadRuns(); pollStats(); shellRefresh(); tutHook('done');
    }
    renderRunPanel(); renderAll(); renderRunPill();
    if (W.teamView === 'chart') requestAnimationFrame(() => layoutAll(false));          // cards grew during the run: keep the rows apart
  }
  if (!['token', 'usage', 'log'].includes(e.kind)) feed(e);
}
function headSoon(){ if (!W.headRaf) W.headRaf = requestAnimationFrame(() => { W.headRaf = 0; renderHead(); }); }
// cards grow while their agents work (assignment, live output): the org chart re-spaces its rows so they never overlap
function chartSoon(){
  if (W.teamView !== 'chart' || W.chartT) return;
  W.chartT = setTimeout(() => { W.chartT = 0;
    const tallest = [...document.querySelectorAll('#nodes .node:not(.atlas):not(.ghost)')].reduce((m, n) => Math.max(m, n.offsetHeight || 0), 0);
    if (Math.abs(tallest - (W.chartTall || 0)) > 6) { W.chartTall = tallest; layoutAll(true); } }, 450);
}
function shellRefresh(){ if (window.shell && shell.refresh) try { shell.refresh(); } catch (_) {} }
function edgeState(id, state){ if (!id) return; const p = $('#edges').querySelector(`[data-e="${id}"]`); if (!p) return; p.classList.remove('draw', 'on', 'done'); p.classList.add(state); }
// after a reload: the last run comes back (steps, output, its activity), and a run still going keeps streaming
async function loadLatestRun(){
  await loadRuns();
  const last = (W.runs || [])[0]; if (!last || (W.run && W.run.id === last.id)) return;
  const r = await api('/runs/' + encodeURIComponent(last.id)); if (!r || r.error || (W.run && W.run.active)) return;
  W.run = {id: r.id, inst: new Map(), active: !!r.active, tin: r.tokens_in || 0, tout: r.tokens_out || 0, task: r.task || '', started: (r.created || 0) * 1000, ended: 0, status: r.active ? '' : (r.status || ''), summary: r.summary || ''};
  W.replay = true;
  try { (r.events || []).forEach(ev => onRunEvent({...ev, run_id: r.id})); } finally { W.replay = false; }
  if (!r.active && !W.run.ended) {                           // stored runs: their end is the last event (or the row's own status)
    const evs = r.events || []; W.run.ended = evs.length ? evs[evs.length - 1].ts * 1000 : W.run.started;
    W.run.status = runFailed(r.status, r.summary) ? (r.status !== 'done' ? r.status : 'error') : (r.status || 'done');
    showSummary(r.id, W.run.status, r.summary || '');
  }
  if (r.active) { setPhase('running'); $('#run-cancel').classList.remove('hide'); $('#tab-n-runs').classList.remove('hide'); attachStream(r.id, 'now'); }
  renderRunPanel(); renderAll(); renderRunPill();
}

/* ------------------------------------------------------------------ approvals */
const AP_KIND = {email: 'email', whatsapp: 'WhatsApp', sms: 'SMS', api_call: 'API call', instagram: 'Instagram'};
async function loadApprovals(){
  if (!W.deskId) return;
  const list = await api('/actions?status=pending') || [];
  const rows = Array.isArray(list) ? list : [];
  const n = rows.length;
  W.apCount = n; W.apList = rows; W.apOldest = n ? ago(Math.min(...rows.map(a => a.created || Date.now() / 1000))) : '';
  $('#ap-n2').textContent = n; $('#ap-n2').className = 'n' + (n ? ' hot' : '');
  $('#tab-n-ap').textContent = n; $('#tab-n-ap').className = 'n' + (n ? ' hot' : '');
  $('#ap-oldest').textContent = n ? 'oldest ' + W.apOldest : '';
  $('#h-queue').innerHTML = n ? `<i class="dot d-warn"></i><span>${n} waiting</span>` : '<i class="dot d-ok"></i><span>none waiting</span>';
  renderApprovals(); renderKpis(); renderHead(); renderExplorer();
}
function renderApprovals(){
  const rows = W.apList || [], n = rows.length;
  const big = $('#ap-panel').parentElement === $('#ap-big');
  const show = big ? rows.slice(0, 80) : rows.slice(0, 1);                 // the right stack keeps room for Atlas: one card, then the queue
  // kind, recipient, age; subject; what the agent wrote (2 lines, 4 in the queue); why it is held and any policy flag as amber callouts
  const card = a => { const kind = AP_KIND[a.kind] || a.kind || 'action'; const body = String(a.body || '').replace(/\s+/g, ' ').trim();
    return `<div class="ap" id="ap-${a.id}"><span class="k">${esc(kind)}</span>
      <div class="l1"><span class="k">${esc(kind)}</span><b title="${esc(a.to || '')}">${esc(a.to || '(no recipient)')}</b><span class="age" title="${esc(fullTs(a.created))}">${ago(a.created)}</span></div>
      ${a.subject ? `<div class="l2" title="${esc(a.subject)}">${esc(a.subject)}</div>` : ''}
      ${body ? (big ? `<div class="apb full"><b>${esc(agentName(a.agent || a.by || 'atlas'))} wrote</b>${esc(String(a.body || '').trim().slice(0, 3000))}</div>`
                    : `<div class="apb" title="${esc(body.slice(0, 600))}"><b>${esc(agentName(a.agent || a.by || 'atlas'))} wrote:</b> ${esc(body.slice(0, 420))}</div>`) : ''}
      ${a.flags ? `<div class="flag"><svg class="i"><use href="#i-alert"/></svg><span><b>Flagged:</b> ${esc(a.flags)}</span></div>` : ''}
      ${a.reason ? `<div class="hold"><b>Held</b> ${esc(a.reason)}</div>` : ''}
      <div class="edit"><input id="ap-s-${a.id}" value="${esc(a.subject || '')}" placeholder="Subject"><textarea id="ap-b-${a.id}">${esc(a.body || '')}</textarea><input class="note" id="ap-n-${a.id}" placeholder="Note for the agents (optional): it goes with Approve or Reject"></div>
      <div class="ap-acts"><button class="btn sm ok" onclick="decide(${a.id},'approved')"><svg class="i"><use href="#i-check"/></svg>Approve &amp; send</button><button class="btn sm" onclick="apEdit(${a.id})"><svg class="i"><use href="#i-edit"/></svg>Edit</button><button class="btn sm d" onclick="decide(${a.id},'rejected')"><svg class="i"><use href="#i-x"/></svg>Reject</button></div></div>`; };
  $('#ap-body').innerHTML = n ? show.map(card).join('') + (n > show.length ? `<div class="ap-more"><span>${plural(n - show.length, 'more message')} waiting</span><button class="link" onclick="showTab('approvals')">Open the queue ›</button></div>` : '')
    : '<div class="none">Nothing waiting. When an agent wants to send something, it lands here first.</div>';
}
function apEdit(id){ const el = document.getElementById('ap-' + id); if (!el) return; el.classList.toggle('editing'); if (el.classList.contains('editing')) $(`#ap-b-${id}`).focus(); }
async function decide(id, status){
  const v = q => { const e = document.getElementById(q); return e ? e.value : ''; };
  const r = await api(`/actions/${id}/decide`, {method: 'POST', body: {status, note: v(`ap-n-${id}`), body: v(`ap-b-${id}`), subject: v(`ap-s-${id}`)}});
  if (!r || r.error) return toast((r && r.error) || 'failed');
  toast(status === 'approved' ? (W.mode === 'demo' ? 'Approved (simulated send in demo mode)' : 'Approved and sent') : 'Rejected: the agents see your note next run');
  loadApprovals(); shellRefresh();
}
function toggleApprovals(){ loadApprovals(); showTab('approvals'); }

/* ------------------------------------------------------------------ tutorial (coach marks driven by what actually happens) */
const TUT = {steps: [], i: -1, auto: true, on: false};
const TUT_STEPS = {
  meet: [
    {t: '#chat-head', h: 'Atlas', p: 'Atlas designs the team, briefs every agent, checks their work and never sends anything without your approval.'},
    {t: '#composer', h: 'Tell it about your business', p: 'One or two sentences: what you do, and the job that eats your time. Try a suggestion if you want a quick start. The team is drafted on the left as you talk.', end: true},
  ],
  team: [
    {t: '#team-panel', h: 'Atlas drafted your team', p: 'Each row is an agent with its own role, standing orders and tools. Click a row to read its brief; Org chart shows the structure.'},
    {t: '#composer', h: 'Reshape it by talking', p: 'Say things like "add a QA reviewer", "split research into a pod of 3", "the writer should report to the researcher". The team changes in place.'},
    {t: '#build-btn', h: 'Build when it looks right', p: 'Building creates the desk with this team. You can still change everything later.', end: true},
  ],
  built: [
    {t: '#newjob-btn', h: 'Give the team a job', p: 'New job (N) opens Runs. Paste a customer enquiry, ask for a report, a comparison, a chase list. Atlas briefs the team and you watch them work in real time.', end: true},
  ],
  running: [
    {t: '#run-panel', h: 'Watch them work', p: 'Each step is an agent: it lights up as it starts, its output streams here, tool calls and hand-backs land in Activity. Click a row in Team to read the full live output.', end: true},
  ],
  cameras: [
    {t: '#camstrip', h: 'Cameras join the desk', p: 'Each tile is a camera. With the journal on, it writes a note whenever something changes, and a summary every 15 minutes. After you build, the picture and the latest note show here.', end: true},
  ],
  watching: [
    {t: '#camstrip', h: 'What the cameras recorded', p: 'Each tile shows the newest frame and the latest written note, with its time. LIVE means the camera is being watched right now.'},
    {t: '#composer', h: 'Ask the cameras', p: 'With "Ask the cameras" selected, type a question like "Was any bag left unattended?". Atlas answers from the journal with times, and shows the frames it used.'},
    {t: '#journal-link', h: 'Read the journal', p: 'Every note, day by day, under the Cameras tab.'},
    {t: '#newjob-btn', h: 'Give the team a job', p: 'Ask for a report or a summary, paste an enquiry, anything. You watch every agent work, and nothing leaves without your approval.', end: true},
  ],
  approval: [
    {t: '#ap-panel', h: 'Nothing goes out without you', p: 'An agent wants to send something. It waits under Approvals: edit the text if you like, then approve or reject with a note. The note tunes the agents next time.', end: true},
  ],
  done: [
    {t: '#run-panel', h: 'The run is done', p: 'The result under Runs is verified by the desk (what was queued, sent, saved), not claimed by the model. Give the team another job, or change the team.', end: true},
  ],
};
function tutDone(){ try { return !!localStorage.getItem('ws_tut_done'); } catch (_) { return false; } }
function tutStart(force, phase){
  if ((tutDone() || isPhone()) && !force) { TUT.auto = false; return; }   // phones: the guide is behind the help button
  TUT.auto = !force;              // a first session is followed through build, the first run and its approval; a replay shows its steps, then stops
  const key = phase || (W.phase === 'run' ? (W.cams.size ? 'watching' : 'built') : (W.agents.size ? 'team' : 'meet'));
  tutShow(TUT_STEPS[key] || TUT_STEPS.meet);
}
function tutHook(ev){ if (!TUT.auto || TUT.on) return; const steps = TUT_STEPS[ev]; if (steps) setTimeout(() => tutShow(steps), ev === 'team' ? 900 : 500); }
function tutShow(steps){ TUT.steps = steps.map(x => ({...x, seen: false})); TUT.i = -1; TUT.on = true; $('#coach').classList.remove('hide'); tutNext(); }
const visible = el => { if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
function tutNext(){
  document.querySelectorAll('.coach-target').forEach(e => e.classList.remove('coach-target'));
  let s; do { TUT.i++; s = TUT.steps[TUT.i]; } while (s && !visible($(s.t)));                // a step whose target is hidden is skipped
  if (!s) { tutEnd(true); return; }
  const target = $(s.t); const box = $('#coach-box');
  const left = TUT.steps.slice(TUT.i + 1).filter(x => visible($(x.t))).length;
  const shown = TUT.steps.slice(0, TUT.i).filter(x => x.seen).length; s.seen = true;     // hidden steps are skipped, and not counted
  $('#coach-step').textContent = `${shown + 1} / ${shown + 1 + left}`; $('#coach-title').textContent = s.h; $('#coach-text').textContent = s.p;
  $('#coach-next').textContent = left ? 'Next' : 'Got it';
  target.classList.add('coach-target'); const r = target.getBoundingClientRect(); const below = r.bottom + 200 < innerHeight;
  box.style.left = Math.max(12, Math.min(innerWidth - 352, r.left)) + 'px'; box.style.top = (below ? r.bottom + 14 : Math.max(12, r.top - 14)) + 'px';
  box.style.transform = below ? '' : 'translateY(-100%)';
  if (!below && r.top - 14 < 200) { box.style.top = Math.max(12, Math.min(innerHeight - 220, r.top + 20)) + 'px'; box.style.transform = ''; box.style.left = Math.max(12, Math.min(innerWidth - 352, r.left - 352)) + 'px'; }
}
function tutEnd(finished){
  TUT.on = false; $('#coach').classList.add('hide'); document.querySelectorAll('.coach-target').forEach(e => e.classList.remove('coach-target'));
  if (finished) { try { localStorage.setItem('ws_tut_done', '1'); } catch (_) {} }   // finishing any walkthrough is enough: it never comes back by itself
}
function tutSkip(){ TUT.auto = false; tutEnd(true); }

/* ------------------------------------------------------------------ cameras: tiles, frames, notes, questions */
function camKind(src){ src = String(src || ''); if (!src) return 'needs a stream address'; if (src.startsWith('sample:') || /AtlasDemo[\\/]videos|[\\/]samples[\\/]/i.test(src)) return 'sample footage'; if (/^rtsps?:/i.test(src)) return 'RTSP camera'; if (/^https?:/i.test(src)) return 'snapshot URL'; if (/^\d+$/.test(src)) return 'webcam'; if (/\.(mp4|mov|avi|mkv|webm|m4v|ts|mpe?g)$/i.test(src)) return 'recording'; if (/\.(jpe?g|png)$/i.test(src)) return 'still image'; return 'camera'; }
async function applyCams(list, instant){
  const incoming = new Map((list || []).map(c => [c.name, c]));
  W.cams.forEach((k, name) => { if (!incoming.has(name)) { if (k.el) { k.el.classList.add('spawn'); setTimeout(() => k.el.remove(), 500); } W.cams.delete(name); if (!instant) addMsg('s', `− camera ${name} removed`, 'assign'); } });
  const first = !W.cams.size && incoming.size;
  for (const [name, c] of incoming) {
    const k = W.cams.get(name);
    if (k) { Object.assign(k, {source: c.source || k.source || '', journal: c.journal !== false, alerts: !!c.alerts, id: c.id || k.id, plan: c.notes || c.focus || k.plan || ''}); renderCamTile(k); continue; }
    const nk = {name, id: c.id || null, source: c.source || '', journal: c.journal !== false, alerts: !!c.alerts, plan: c.notes || c.focus || '', el: null, st: c.id ? 'off' : 'draft', frameTs: 0};
    const el = document.createElement('div'); el.className = 'cam' + (instant ? '' : ' spawn'); el.dataset.name = name;
    el.innerHTML = `<div class="pic"><span class="kind"></span><span class="badge hide"></span><span class="cnt"></span></div><div class="cn"><b>${esc(name)}</b><span class="tag"></span></div><div class="note"></div>`;
    el.onclick = () => camClick(name);
    $('#cams').insertBefore(el, $('#cam-add')); nk.el = el; W.cams.set(name, nk); renderCamTile(nk);
    if (!instant) { addMsg('s', `+ camera ${name}: ${camKind(c.source)}${nk.journal ? ', keeping a journal' : ''}`, 'assign'); void el.offsetWidth; await sleep(30); el.classList.remove('spawn'); await sleep(170); }
  }
  $('#camstrip').classList.toggle('hide', !incoming.size); $('#camstrip').classList.toggle('has', incoming.size > 0);
  $('#cams').style.setProperty('--n', Math.max(1, incoming.size)); $('#cams').style.setProperty('--cols', Math.min(6, Math.max(1, incoming.size)));
  $('#cam-none').classList.toggle('hide', incoming.size > 0);
  fitCams();
  if (first) { layoutAll(!instant); }
  if (W.phase === 'run') setMode(W.modeSel);
  renderAll();
  if (first && !instant) tutHook('cameras');
}
// the server's one answer per camera (api /cameras: status, last_note, journal) - the same truth Home reads
function ingestCams(r){
  if (!r || !Array.isArray(r.cameras)) return;
  W.vlmReady = !!(r.vlm && r.vlm.ready);
  let lastNote = 0;
  for (const c of r.cameras) {
    const k = W.cams.get(c.name); if (!k) continue;
    const st = c.status || {}; const s = c.seen || {}; const le = c.last_event || {};
    Object.assign(k, {id: c.id, st: st.state || (c.watch_job && c.watch_job.enabled ? 'waiting' : 'off'), watching: !!st.watching, every: st.every_s || 30,
      frameTs: st.frame_ts || s.ts || le.ts || 0, problem: st.problem || '', sourceKind: c.source_kind || '', journal: !!c.journal,
      counts: s.counts || le.counts || {}, lastNote: c.last_note || null});
    if (c.last_note && c.last_note.ts > lastNote) lastNote = c.last_note.ts;
    renderCamTile(k);
  }
  W.lastNoteTs = lastNote;
  renderKpis(); renderHead(); renderExplorer(); renderCamSide(); stamp();
}
function renderCamTile(k){
  const el = k.el; if (!el) return;
  const pic = el.querySelector('.pic'), kind = pic.querySelector('.kind'), badge = pic.querySelector('.badge'), cnt = pic.querySelector('.cnt'), tag = el.querySelector('.cn .tag'), note = el.querySelector('.note');
  const built = !!k.id;
  if (built && k.frameTs && k.frameTs !== k.shownTs) {                       // the newest real frame (live, or the last one kept)
    k.shownTs = k.frameTs;
    const src = withDesk(`/api/cameras/${k.id}/frame.jpg?t=${Math.round(k.frameTs * 1000)}`);
    const pre = new Image(); pre.onload = () => { let img = pic.querySelector('img'); if (!img) { img = document.createElement('img'); img.alt = ''; pic.insertBefore(img, pic.firstChild); } img.src = src; pic.classList.add('has'); }; pre.src = src;
  }
  kind.textContent = !built ? (k.source ? camKind(k.source) + ' · goes live when the desk is built' : 'needs a stream address')
    : (k.problem ? 'feed settings unreadable' : (k.source || k.sourceKind ? 'no frame yet' : 'needs a stream address'));
  const st = built ? k.st : 'draft';
  badge.className = 'badge ' + (st === 'live' ? 'live' : st === 'waiting' ? 'waiting' : 'off');
  badge.innerHTML = st === 'live' ? 'live' : k.frameTs ? esc(fmtTs(k.frameTs)) + '<span class="z">&nbsp;UTC</span>' : '';   // no frame: the tile itself says so
  badge.classList.toggle('hide', !badge.textContent);
  badge.title = k.frameTs ? (st === 'live' ? 'Live · newest frame ' : 'Not live · this frame is from ') + fullTs(k.frameTs) : '';
  // one chip per kind of object (the three most numerous): they wrap on a narrow tile instead of being cut
  const cl = built && k.frameTs ? countsText(k.counts) : '';
  const cnth = cl ? cl.split(', ').slice(0, 3).map(t => `<span>${esc(t)}</span>`).join('') : '';
  if (cnt.innerHTML !== cnth) { cnt.innerHTML = cnth; cnt.title = cl; }
  const tg = !built ? ['draft', ''] : k.problem ? ['fix feed', 'warn'] : st === 'live' ? ['watching', 'ok'] : st === 'waiting' ? ['starting', 'warn'] : ['not watching', ''];
  tag.textContent = tg[0]; tag.className = 'tag ' + tg[1]; tag.title = k.problem || (st === 'off' && built ? 'The watch job for this camera is switched off' : '');
  note.classList.remove('warn');
  if (k.lastNote && k.lastNote.text) { const t = `<b title="${esc(fullTs(k.lastNote.ts))}">${esc(fmtTs(k.lastNote.ts))}</b>`; const html = t + esc(k.lastNote.text); if (note.innerHTML !== html) { const fresh = k.noteTs && k.noteTs !== k.lastNote.ts; note.innerHTML = html; k.noteTs = k.lastNote.ts; if (fresh) { note.classList.remove('new'); void note.offsetWidth; note.classList.add('new'); } } }
  else if (!built) note.textContent = k.plan || 'Planned by Atlas';
  else if (k.problem) { note.textContent = 'Open the feed and re-enter the stream address to start watching.'; note.classList.add('warn'); }
  else note.textContent = k.journal ? 'No journal note yet.' : 'Journal off: detections only.';
}
function camClick(name){ const k = W.cams.get(name); if (!k) return; if (k.id && k.st === 'live') openLive(name); else openAttach(name); }
function startCamPoll(){ clearInterval(W.camPoll); W.evSince = Date.now() / 1000 - 5; pollCams(true); W.camPoll = setInterval(() => { if (!document.hidden) pollCams(); }, 4000); }
async function pollCams(withNotes){
  if (!W.cams.size || !W.deskId) return;
  const r = await api('/cameras'); if (!r || !r.cameras) return;
  const known = new Set(W.cams.keys()); const extra = r.cameras.filter(c => !known.has(c.name));
  if (extra.length) await applyCams([...[...W.cams.values()].map(c => ({name: c.name, id: c.id, source: c.source, journal: c.journal, alerts: c.alerts})), ...extra.map(c => ({name: c.name, id: c.id, source: (c.config || {}).source || '', journal: !!c.journal}))], true);
  ingestCams(r);
  if (withNotes || !W.notesT || Date.now() - W.notesT > 30000) {           // Home's "Camera notes · 24h": events with a written answer
    W.notesT = Date.now(); const v = await api('/vision/events?hours=24&limit=400'); if (Array.isArray(v)) { W.notes24 = v.filter(e => e.answer).length; renderKpis(); }
    if (W.cams.size <= 2) { const r30 = await api('/vision/events?hours=720&limit=60'); if (Array.isArray(r30)) { W.recentNotes = r30.filter(e => e.answer).sort((a, b) => b.ts - a.ts).slice(0, 8); renderCamSide(); } }
  }
  const ev = await api('/vision/events?hours=1&limit=20') || [];
  if (Array.isArray(ev)) ev.filter(e => e.ts > W.evSince && (e.source === 'journal' || e.source === 'digest')).sort((a, b) => a.ts - b.ts).forEach(e => {
    W.evSince = Math.max(W.evSince, e.ts);
    feed({ts: e.ts, agent: e.camera, kind: e.source === 'digest' ? 'summary' : 'journal', text: e.answer || e.reason || '', data: {}});
    const k = W.cams.get(e.camera); if (k && k.el) { k.el.classList.remove('flash'); void k.el.offsetWidth; k.el.classList.add('flash'); setTimeout(() => k.el.classList.remove('flash'), 900); }
  });
}
// one or two cameras leave the strip half empty: beside them, what needs fixing and the newest written notes (30 days)
function renderCamSide(){
  const few = W.phase === 'run' && W.cams.size > 0 && W.cams.size <= 2 && !isPhone();
  $('#camstrip').classList.toggle('few', few); if (!few) return;
  const fixes = [...W.cams.values()].filter(k => k.id && k.problem), notes = W.recentNotes || [];
  const html = (fixes.length ? `<div class="gk">Needs attention</div>` + fixes.map(k => `<div class="cs-row"><svg class="i i14"><use href="#i-alert"/></svg><span><b>${esc(k.name)}</b> ${esc(k.problem)}</span><button class="btn sm" onclick="openAttach(${jsq(k.name)})">Fix feed</button></div>`).join('') : '')
    + `<div class="gk">Newest notes · UTC</div>` + (notes.length ? notes.map(e => `<div class="cs-note" title="${esc(fullTs(e.ts))}"><span class="ts">${esc(fmtTs(e.ts))}</span><b>${esc(e.camera)}</b><span class="tx">${esc(e.answer)}</span></div>`).join('')
      : `<div class="none">${W.recentNotes ? 'No written notes in the last 30 days. Notes appear while a camera is watched with its journal on.' : 'Reading the journal…'}</div>`);
  if (html !== W.camSideHtml) { W.camSideHtml = html; $('#cam-side-in').innerHTML = html; }   // the poll repaints only what changed
}
function stamp(){ W.updatedAt = Date.now(); $('#tab-upd').textContent = 'Updated ' + hhmmss() + ' UTC'; }
async function askCams(text, already, hours){
  if (!already) addMsg('u', text);
  setSugg([]);
  W.busy = true; $('#send').disabled = true; setState('reading journal');
  const r = await api('/vision/ask', {method: 'POST', body: {question: text, hours: hours || 24}});
  W.busy = false; $('#send').disabled = false; setState('ready');
  if (!r || r.error) { addMsg('s', (r && r.error) || 'could not read the journal; try again'); setSugg(CAM_QUESTIONS); return; }
  const d = addMsg('a', r.answer || 'Nothing in the journal answers that yet.');
  d.classList.add('camans');
  const ev = (r.evidence || []).filter(e => e.snapshot_url).slice(-6);
  if (ev.length) {
    const row = document.createElement('div'); row.className = 'evid';
    row.innerHTML = ev.map(e => `<a href="${esc(withDesk(e.snapshot_url))}" target="_blank" rel="noopener" title="${esc(e.camera)} #${e.id}${e.ts ? ' · ' + esc(fullTs(e.ts)) : ''}"><img src="${esc(withDesk(e.snapshot_url))}" loading="lazy" alt=""><span>${e.ts ? esc(fmtTs(e.ts)) : '#' + e.id} ${esc(e.camera)}</span></a>`).join('');
    d.appendChild(row); $('#msgs').scrollTop = 1e9;
    [...new Set(ev.map(e => e.camera))].forEach(n => { const k = W.cams.get(n); if (k && k.el) { k.el.classList.add('flash'); setTimeout(() => k.el.classList.remove('flash'), 1400); } });
  }
  const m = r.retrieval || {}; if (m.considered) addMsg('s', `searched ${m.considered} journal entries (${m.window || 'last 24h'})${m.grounding ? ', ' + m.grounding : ''}`);
  if (!m.considered && (!hours || hours <= 24) && W.lastNoteTs && W.lastNoteTs < Date.now() / 1000 - 86400) {
    addMsg('s', `nothing logged in the last 24 hours · the newest note is from ${fmtTs(W.lastNoteTs)} UTC`);
    return setSugg([{label: 'Search the last 30 days', run: () => askCams(text, true, 720)}, ...CAM_QUESTIONS]);
  }
  setSugg(CAM_QUESTIONS);
}
async function loadJournal(){
  if (!W.deskId) return;
  $('#journal-page').href = withDesk('/api/vision/journal?format=html');
  const ev = await api('/vision/events?hours=720&limit=500'); const box = $('#journal-list'); if (!Array.isArray(ev)) return;
  const notes = ev.filter(e => e.answer).sort((a, b) => b.ts - a.ts).slice(0, 80);
  $('#jr-meta').textContent = notes.length ? `${plural(notes.length, 'note')}, newest first · since ${fmtTs(notes[notes.length - 1].ts).replace(/ \d\d:\d\d$/, '') || 'today'} · UTC` : 'written notes from the cameras · UTC';
  let day = ''; const out = [];
  notes.forEach(e => { const dl = dayLabel(e.ts); if (dl !== day) { day = dl; out.push(`<div class="jday">${esc(dl)}</div>`); }
    out.push(`<div class="jrow"><span class="ts" title="${esc(fullTs(e.ts))}">${new Date(e.ts * 1000).toISOString().slice(11, 16)}</span><span class="cm">${esc(e.camera)}<small>${e.source === 'digest' ? 'summary' : e.source === 'journal' ? 'note' : 'answer'}</small></span><div class="tx">${esc(e.answer)}</div></div>`); });
  box.innerHTML = out.length ? out.join('') : `<div class="none">${W.cams.size ? 'No written notes in the last 30 days. Notes appear while a camera is being watched with its journal on.' : 'No cameras on this desk yet.'}</div>`;
}

/* ------------------------------------------------------------------ live view of one camera (motion JPEG with detections) */
// one attempt per open (W.liveSeq): a failure is handled once, the stream is dropped without firing another error, and
// nothing reconnects by itself - the owner retries or changes the feed
function liveStop(){ const img = $('#live-img'); img.onerror = img.onload = null; img.removeAttribute('src'); clearInterval(W.liveChk); clearTimeout(W.liveT); }
function openLive(name){
  const k = W.cams.get(name); if (!k) return;
  if (!k.id) return toast('This camera goes live once the desk is built');
  liveStop();
  const seq = W.liveSeq = (W.liveSeq || 0) + 1;
  W.liveCam = name;
  $('#live-name').textContent = name; $('#live-k').textContent = 'Live camera · ' + (k.sourceKind || camKind(k.source));
  liveNote();
  const img = $('#live-img'); img.style.visibility = 'hidden';
  $('#live-state').innerHTML = `<div><b>Connecting to ${esc(name)}…</b>the first frame usually takes a few seconds</div>`; $('#live-state').classList.remove('hide');
  img.onerror = () => liveFail(k, seq);
  img.src = withDesk(`/api/cameras/${k.id}/live.mjpg?fps=12&t=${Date.now()}`);
  W.liveChk = setInterval(() => { if (W.liveSeq === seq && img.naturalWidth > 0) { img.style.visibility = 'visible'; $('#live-state').classList.add('hide'); clearInterval(W.liveChk); clearTimeout(W.liveT); } }, 400);
  W.liveT = setTimeout(() => { if (!(img.naturalWidth > 0)) liveFail(k, seq); }, 6500);
  $('#liveview').classList.remove('hide');
}
async function liveFail(k, seq){
  if (W.liveSeq !== seq || W.liveFailed === seq || W.liveCam !== k.name) return;
  W.liveFailed = seq; liveStop();
  $('#live-state').classList.remove('hide');
  $('#live-state').innerHTML = `<div><b>No picture from ${esc(k.name)}</b>reading why…</div>`;
  const s = await api(`/cameras/${k.id}/live`); if (W.liveSeq !== seq) return;
  const why = k.problem || (s && s.live && s.live.error) || (s && s.error) || 'The source sent no frames.';
  $('#live-state').innerHTML = `<div><b>No picture from ${esc(k.name)}</b>${esc(why)}<br><button class="btn sm" onclick="openLive(${jsq(k.name)})"><svg class="i"><use href="#i-reset"/></svg>Try again</button> <button class="btn sm" onclick="closeLive();openAttach(${jsq(k.name)})"><svg class="i"><use href="#i-cam"/></svg>Change feed</button></div>`;
}
function liveNote(){ const k = W.cams.get(W.liveCam); if (!k) return; const n = k.lastNote; $('#live-note-k').textContent = n ? 'Latest journal note · ' + fmtTs(n.ts) + ' UTC' : 'Latest journal note'; $('#live-note').textContent = n ? n.text : 'No journal note yet. Notes are written while the camera is watched with its journal on.'; }
function closeLive(){ liveStop(); W.liveSeq = (W.liveSeq || 0) + 1; $('#liveview').classList.add('hide'); W.liveCam = null; }
setInterval(() => { if (W.liveCam) liveNote(); }, 2000);

/* ------------------------------------------------------------------ your desks, guide */
async function loadMyDesks(){
  const r = await api('/desks'); const all = (r && r.desks) || [];
  W.desks = all; renderHead();
  const list = all.slice().sort((a, b) => (b.created || 0) - (a.created || 0));
  $('#desks-n').textContent = list.length;
  $('#desks-panel').classList.toggle('hide', !list.length);
  $('#other-panel').classList.toggle('hide', !!list.length);              // no desk yet: the ways in that skip the conversation
  const day = ts => ts ? new Date(ts * 1000).toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric'}) : '';
  $('#mydesks').innerHTML = list.map(d => `<button onclick="openDesk(${d.id})"><span class="sw"></span><span><b>${esc(d.business_name || d.name)}</b><small>${esc((d.template || 'custom').replace(/_/g, ' '))} · ${esc(d.tier || 'free')} models · created ${esc(day(d.created))}</small></span><span class="c">#${d.id}</span><span></span><svg class="i i14 go"><use href="#i-chev"/></svg></button>`).join('');
}
function openDesk(id){ location.href = '/desk/workspace?desk=' + id; }
function openGuide(){ $('#guide').classList.remove('hide'); }
function closeGuide(){ $('#guide').classList.add('hide'); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (document.getElementById('sh-pal') || (e.target && e.target.closest && e.target.closest('#sh-pal'))) return;   // the frame's palette closes itself
    if (!$('#guide').classList.contains('hide')) return closeGuide();
    if (!$('#namedlg').classList.contains('hide')) return closeDeskName();
    if (!$('#attach').classList.contains('hide')) return closeAttach();
    if (!$('#liveview').classList.contains('hide')) return closeLive();
    if (TUT.on) return tutEnd(true);
    if ($('#ws').classList.contains('atlas-open')) return closeAtlas();
    if ($('#ws').classList.contains('ex-open')) return toggleExplorer(false);
    if ($('#insp').classList.contains('on')) return closeInsp();
    return;
  }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '') || (e.target && e.target.isContentEditable);
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); openAtlas(); }
  else if ((e.key === 'n' || e.key === 'N') && W.phase === 'run') { e.preventDefault(); newJob(); }
});

/* ------------------------------------------------------------------ free / paid models (always the owner's click) */
async function setTier(tier){
  if (!W.sid || W.deskId) return toast(W.deskId ? 'This desk runs on ' + (W.tier === 'free' ? 'free' : 'paid') + ' models; change it in Desk setup' : 'Start a conversation first');
  const r = await api(`/design/${W.sid}/tier`, {method: 'POST', body: {tier}});
  if (!r || r.error) return toast((r && r.error) || 'could not switch');
  W.tier = r.tier; setDeskStatus(); renderAll();
  addMsg('s', W.tier === 'free' ? 'switched to free models' : 'switched to paid models: Claude for design, a paid vision model for the cameras', 'assign');
}
function toggleTier(){ return setTier(W.tier === 'free' ? 'balanced' : 'free'); }

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
    if (ev.button !== 0) return;
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
  $('#at-kicker').textContent = !cam ? 'New camera' : cam.problem ? 'Camera feed · settings unreadable, re-enter the address' : (cam.source ? 'Camera feed · ' + camKind(cam.source) : (cam.sourceKind ? 'Camera feed · ' + cam.sourceKind : 'Camera feed · not connected'));
  $('#at-name-row').classList.toggle('hide', !!cam); $('#at-cam-name').value = '';
  ['#at-rtsp', '#at-http'].forEach(q => { $(q).value = ''; });
  const src = cam && cam.source || '';
  if (src) { const t = srcTab(src); if (t === 'rtsp') $('#at-rtsp').value = src; else if (t === 'http') $('#at-http').value = src; else if (t === 'webcam') $('#at-webcam').value = src; else if (t === 'upload') { AT.uploaded = src; AT.uploadedName = src.split(/[\\/]/).pop(); } else if (t === 'sample') AT.sample = src; }
  atPreview(cam && cam.id && cam.frameTs ? withDesk(`/api/cameras/${cam.id}/frame.jpg?t=${Date.now()}`) : '', cam && cam.id && cam.frameTs ? `Newest frame from this camera · ${fmtTs(cam.frameTs)} UTC` : '');
  $('#at-live').classList.toggle('hide', !(cam && cam.id && cam.st === 'live'));
  $('#at-go-t').textContent = cam && (cam.source || cam.id) ? 'Switch feed' : 'Attach feed';
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
  const r = await api('/cameras/samples'); AT.samples = (r && r.samples) || null;            // the clips on this server: no desk needed
  const box = $('#at-samples');
  box.innerHTML = AT.samples && AT.samples.length ? AT.samples.map(c => `<button data-s="sample:${esc(c.name)}" onclick="pickSample(this.dataset.s)"><span>${esc(c.label)}</span><span class="c">${esc(c.name)}</span></button>`).join('')
    : `<div class="hint" style="padding:10px">${AT.samples ? 'No sample footage is installed on this server.' : 'Could not read the sample list. Try again in a moment.'}</div>`;
  if (AT.sample) markSample();
}
function markSample(){ document.querySelectorAll('#at-samples button').forEach(b => b.classList.toggle('on', b.dataset.s === AT.sample || (AT.sample && !AT.sample.startsWith('sample:') && AT.sample.includes(b.dataset.s.slice(7))))); }
function pickSample(s){ AT.sample = s; markSample(); $('#at-test').classList.remove('hide'); testAttach(); }
function uploadAttach(file){
  if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const xhr = new XMLHttpRequest(); xhr.open('POST', '/api/cameras/upload');
  if (deskOf()) xhr.setRequestHeader('X-Desk', String(deskOf()));
  $('#at-prog').classList.remove('hide'); $('#at-bar').style.width = '0%'; $('#at-ptext').textContent = `uploading ${file.name}`;
  xhr.upload.onprogress = e => { if (e.lengthComputable) { const pc = Math.round(e.loaded * 100 / e.total); $('#at-bar').style.width = pc + '%'; $('#at-ptext').textContent = `uploading ${file.name} · ${pc}%`; } };
  xhr.onload = () => {
    let r = {}; try { r = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
    if (xhr.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search); return; }
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
      const nk = W.cams.get(name); Object.assign(nk, {id: r.camera.id, source: src, problem: '', st: 'waiting', watching: true}); renderCamTile(nk);
      if (W.phase === 'run') { startCamPoll(); setMode(W.modeSel); }
      addMsg('s', `camera ${name} → ${camKind(src)}`, 'assign');
    } else {                                                                            // not built yet: it goes into the draft
      if (!W.bp) W.bp = {agents: [], cameras: []};
      const list = W.bp.cameras = W.bp.cameras || []; const c = list.find(x => x.name === name);
      if (c) c.source = src; else list.push({name, source: src, journal: true, notes: '', focus: ''});
      W.addedCams.set(name, src);
      if (W.phase === 'meet') openWorkspace();
      await applyCams(list);
      addMsg('s', `camera ${name} → ${camKind(src)} (goes live when the desk is built)`, 'assign');
      $('#build-btn').disabled = !((W.bp.agents || []).length);
    }
    toast(`${name}: ${camKind(src)} attached`); closeAttach();
  } finally { $('#at-go').disabled = false; }
}
function atLive(){ const n = AT.name; closeAttach(); if (n) openLive(n); }
(function dropZone(){
  const d = document.getElementById('at-drop'); if (!d) return;
  ['dragenter', 'dragover'].forEach(t => d.addEventListener(t, e => { e.preventDefault(); d.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => d.addEventListener(t, e => { e.preventDefault(); d.classList.remove('over'); }));
  d.addEventListener('drop', e => { const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (f) uploadAttach(f); });
})();

/* ------------------------------------------------------------------ console: header, stat strip, team table, run panel, tabs, explorer */
const fmtDur = s => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
function agentState(id){
  if (id === 'atlas') return W.run && W.run.active ? {cls: 'run', dot: 'd-run', label: 'Leading', turns: ''} : {cls: W.phase === 'run' ? 'ready' : 'q', dot: W.phase === 'run' ? 'd-ok' : 'd-q', label: W.phase === 'run' ? 'Ready' : 'Lead', turns: ''};
  if (W.run) { const st = [...W.run.inst.values()].filter(s => s.base === id); if (st.length) { const s = st[st.length - 1];
    const map = {writing: ['run', 'd-run', 'Writing'], thinking: ['run', 'd-think', 'Thinking'], done: ['ok', 'd-ok', 'Done'], error: ['err', 'd-err', 'Error'], idle: ['q', 'd-q', 'Queued']};
    const [cls, dot, label] = map[s.status] || ['q', 'd-q', s.status]; return {cls, dot, label: label + (s.turns && s.status !== 'done' ? ' · t' + s.turns : ''), turns: s.turns}; } }
  return {cls: W.phase === 'run' ? 'ready' : 'q', dot: W.phase === 'run' ? 'd-ok' : 'd-q', label: W.phase === 'run' ? 'Ready' : 'Drafted', turns: ''};
}
// what stops an agent doing part of its job: shown beside its run state, whatever the last run did
const agentGaps = id => (W.phase === 'run' && W.gaps && W.gaps.get(id)) || [];
function workingCount(){ return W.run && W.run.active ? [...W.run.inst.values()].filter(s => ['writing', 'thinking'].includes(s.status)).length : 0; }
function renderAll(){ renderHead(); renderKpis(); renderTeamTable(); renderExplorer(); }
function renderHead(){
  const n = W.agents.size, built = !!W.deskId, cams = [...W.cams.values()];
  const date = new Date().toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'});
  const eb = W.phase === 'load' ? 'Atlas workspace · loading the desk'
    : built ? `Atlas workspace · desk #${W.deskId} · ${(W.template || 'custom').replace(/_/g, ' ')} · ${W.tier === 'free' ? 'free' : 'paid'} models`
    : n ? 'Atlas workspace · draft, not built' : `Atlas workspace · ${(W.desks || []).length ? 'new desk' : 'no desk yet'}`;
  $('#ph-k').innerHTML = `<span class="eb-d">${esc(date)} · </span>${esc(eb)}`;
  $('#ph-title').textContent = W.phase === 'load' ? 'Loading the desk…' : (W.deskName || 'New desk');
  document.title = (W.deskName ? W.deskName + ' · ' : '') + 'Atlas workspace';
  let lead = '';
  if (W.phase === 'meet') lead = 'No draft yet. Nothing is built until you press <b>Build desk</b>.';
  else if (W.phase === 'design') lead = `Draft: Atlas + ${plural(n, 'specialist')}${cams.length ? ' · ' + plural(cams.length, 'camera') : ''}, not built yet. Build it when it looks right, or keep describing changes.`;
  else if (W.phase === 'run') {
    const gaps = gapCount(), need = [];
    if (W.apCount) need.push(`${plural(W.apCount, 'approval')} waiting · oldest ${W.apOldest}`);
    if (gaps) need.push(`${plural(gaps, 'agent gap')} to fix`);
    const ap = W.apCount == null ? '' : need.length ? `<b class="warn">${esc(need.join(' · '))}.</b> ` : '<b class="ok">No approvals waiting.</b> ';
    const wk = workingCount();
    const team = W.run && W.run.active ? `Atlas + ${plural(n, 'specialist')}, ${wk} working on ${esc(W.run.id)}` : `Atlas + ${plural(n, 'specialist')} ready`;
    const live = cams.filter(c => c.st === 'live').length;
    const camBit = cams.length ? ` · ${live} of ${plural(cams.length, 'camera')} live` : '';
    const day = Date.now() / 1000 - 86400, jobs = (W.runs || []).filter(r => r.created > day).length;
    lead = `${ap}${team}${camBit} · ${plural(jobs, 'job')} in 24 h.`;
  }
  $('#ph-sum').innerHTML = lead || '&nbsp;';
  $('#tab-n-team').textContent = n + 1; $('#team-n').textContent = n + 1;
  renderSteps();
  $('#tab-n-cams').textContent = W.cams.size; $('#cam-n').textContent = W.cams.size; $('#cam-n2').textContent = W.cams.size;
  $('#team-meta').textContent = !n ? 'drafted here as you talk to Atlas' : (W.teamView === 'chart' ? 'drag cards to arrange · drag the background to pan · Ctrl + scroll to zoom'
    : (W.phase === 'run' ? `Atlas + ${plural(n, 'specialist')} · click a row for the brief` : `draft · Atlas + ${plural(n, 'specialist')} · click a row for the brief`));
  const live = cams.filter(c => c.st === 'live').length, watching = cams.filter(c => c.watching).length;
  $('#cam-meta').textContent = !cams.length ? '' : !W.deskId ? 'planned · each camera keeps a written journal once built' : (live ? `${live} live · newest frame and note per camera · UTC` : watching ? 'watching, waiting for frames · UTC' : 'not being watched · newest frame and note kept · UTC');
  refreshGreeting();
  $('#cam-meta2').textContent = cams.length ? `${live} of ${cams.length} live · detections by the local detector${W.vlmReady ? ' · notes by the vision model' : ''} · times UTC` : 'frames through the local detector';
  renderRunPill();
}
// the four steps with where this desk stands: brief, shape the team, cameras (optional), build
function renderSteps(){
  const has = W.agents.size > 0, cams = W.cams.size > 0;
  const st = W.phase === 'meet' ? ['on', '', cams ? 'done' : '', ''] : W.phase === 'design' ? ['done', has ? 'done' : 'on', cams ? 'done' : '', has ? 'on' : ''] : ['done', 'done', cams ? 'done' : '', 'done'];
  document.querySelectorAll('#start-panel .st2').forEach((el, i) => { el.className = 'st2 ' + (st[i] || ''); const n = el.querySelector('.sno');
    if (n) n.innerHTML = st[i] === 'done' ? '<svg class="i i12"><use href="#i-check"/></svg>' : String(i + 1); });
}
// no desk yet: the two ways in that skip the conversation, on Home
function otherWay(k){ if (k === 'template' && window.shell && shell.go) return shell.go('/desk', 'showOnboardingForm'); location.href = '/desk'; }
function renderRunPill(){
  const local = W.run && W.run.active, n = Math.max(W.activeRuns || 0, local ? 1 : 0);
  const p = $('#run-pill'); p.className = 'pill' + (local ? ' live' : n ? ' ok' : '');
  $('#run-n').textContent = n; $('#sb-run-t').classList.toggle('hide', !local);
  p.title = local ? `Run ${W.run.id} in progress: open Runs` : 'Runs in progress on this desk';
}
function kpi(sel, v, small, s, cls){ const el = $(sel); if (!el) return; el.className = 'kpi' + (cls ? ' ' + cls : ''); el.querySelector('.v').innerHTML = esc(String(v)) + (small ? `<small>${esc(small)}</small>` : ''); if (s !== undefined) { const e = el.querySelector('.s'); e.textContent = s || '\u00a0'; e.title = s || ''; } }
function renderKpis(){
  if (W.phase === 'load') return;
  const nSpec = W.agents.size, n = nSpec ? nSpec + 1 : 0, wk = workingCount();
  kpi('#k-agents', n, '', !n ? 'no team yet' : (W.run && W.run.active ? `${wk} working now · Atlas + ${plural(nSpec, 'specialist')}` : `Atlas + ${plural(nSpec, 'specialist')}`));
  const cams = [...W.cams.values()], live = cams.filter(c => c.st === 'live').length, watching = cams.filter(c => c.watching).length, fixes = cams.filter(c => c.problem).length;
  const lastF = cams.reduce((m, c) => Math.max(m, c.frameTs || 0), 0);
  if (!cams.length) kpi('#k-cams', 0, '', 'none attached');
  else kpi('#k-cams', live, `of ${cams.length}`, live === cams.length ? 'all being watched' : fixes ? `${plural(fixes, 'feed')} to fix` : !watching ? (lastF ? 'not watched · frame ' + fmtTs(lastF) : 'not being watched') : `${watching - live} waiting for frames`, live < cams.length ? 'warn' : 'ok');
  kpi('#k-journal', W.notes24 == null ? (cams.length ? '–' : 0) : W.notes24, '', W.lastNoteTs ? 'last ' + fmtTs(W.lastNoteTs) + ' UTC' : (cams.length ? 'no notes yet' : 'no cameras'));
  kpi('#k-ap', W.apCount == null ? '–' : W.apCount, '', W.apCount ? 'oldest ' + W.apOldest : 'nothing outbound queued', W.apCount ? 'warn' : '');
  // the server's figure in the server's words (the same on Home): a free-tier desk leads with "Free", the paid-rate
  // estimate under it; the qualifier leads the sub-line, so it never truncates away
  const c = W.cost;
  if (c) { kpi('#k-spend', c.value || '£' + Number(c.gbp || 0).toFixed(2), c.small || '', c.note || 'at paid rates'); $('#k-spend').title = c.title || ''; }
  else kpi('#k-spend', '–', '', 'not reported yet');
  const lb = $('#k-spend .l'); if (lb && c && c.label) { lb.querySelector('.t-l').textContent = c.label; lb.querySelector('.t-s').textContent = c.short || c.label; }
}
function goalOf(id, a){ if (id !== 'atlas') return a.goal || ''; const ac = atlasCfg(); return (ac && ac.goal) || 'Briefs the team, runs leads in parallel, reviews every result, and holds anything outbound for your approval.'; }
function renderTeamTable(){
  const rows = []; const ids = ['atlas', ...W.order];
  const ac = atlasCfg();
  for (const id of ids) {
    const a = id === 'atlas' ? {name: 'Atlas', role: 'Orchestrator · leads the team', tools: ac ? (ac.tools || []).filter(t => !['list_agents', 'finish'].includes(t)) : null, engine: (ac && ac.engine) || 'atlas', engine_note: (ac && ac.engine_note) || '', color: ''} : W.agents.get(id); if (!a) continue;
    const st = agentState(id);
    const member = a.reports_to && a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? (W.agents.get(a.reports_to) || {}).name : '';
    const tools = a.tools; const eng = engineLabel(a); const gaps = agentGaps(id);
    // the tools: two in a one-line row (+N), three or six, wrapping, when the table has room (fitTeam d2 / d3)
    const more = (from, cls) => tools && tools.length > from ? `<span class="chip ${cls}" title="${esc(tools.slice(from).join(', '))}">+${tools.length - from}</span>` : '';
    const toolCell = tools == null ? '<span class="muted" style="font-size:11.5px">set at build</span>' : tools.length ? `<div class="tools-c">${tools.slice(0, 6).map((t, i) => `<span class="chip${i > 1 ? ' x' : ''}${i > 2 ? ' x3' : ''}">${esc(t)}</span>`).join('')}${more(2, 'more')}${more(3, 'more2')}${more(6, 'more3')}</div>` : '<span class="muted">none</span>';
    const orders = (id === 'atlas' ? ((ac && ac.instructions) || []) : (a.instructions || [])).slice(0, 3);
    rows.push(`<tr id="tr-${esc(id)}" class="${W.sel === id ? 'sel' : ''}" onclick="inspect(${jsq(id)})"><td><div class="obj" style="--c:${esc(a.color || '')}">${id === 'atlas' ? '<span class="amark"></span>' : '<span class="sw"></span>'}<div><b>${esc(a.name)}</b><span>${esc(a.role || '')}${member ? ' · reports to ' + esc(member) : ''}</span><small class="goal" title="${esc(goalOf(id, a))}">${esc(goalOf(id, a))}</small>${orders.length ? `<ul class="ord">${orders.map(x => `<li title="${esc(x)}">${esc(x)}</li>`).join('')}</ul>` : ''}</div></div></td>` +
      `<td><div class="stc"><span class="st ${st.cls}"><i class="dot ${st.dot}"></i>${esc(st.label)}</span>${gaps.length ? `<span class="gapc" title="${esc(gaps.join('; '))}">${gaps.length === 1 ? 'gap' : gaps.length + ' gaps'}</span>` : ''}</div></td><td><span class="eng" title="${esc(eng.title)}">${esc(eng.t)}</span></td><td class="num">${st.turns || '<span class="muted">–</span>'}</td>` +
      `<td>${toolCell}</td></tr>`);
  }
  $('#team-rows').innerHTML = rows.join(''); $('#team-empty').classList.toggle('hide', W.agents.size > 0);
  $('#team-empty').textContent = W.phase === 'run' ? 'Only Atlas so far. Add a specialist on the team canvas, or choose Change the team in the Atlas panel.'
    : 'No specialists yet. Atlas drafts them here from your brief, one row per agent; click a row to read its brief.';
  if ((W.agents.size + 1) !== W.fitRows) { W.fitRows = W.agents.size + 1; fitTeam(); }
}
// Table or Org chart, remembered separately for a draft and for a built desk. A draft opens on the Org chart (the team is
// drawn as Atlas drafts it), except in one column where the chart cannot show the team whole; a built desk opens on the
// table. Before the first draft there is only Atlas: the table.
const tvKey = () => W.phase === 'design' ? 'ws_teamview_draft' : 'ws_teamview';
function viewFor(){
  if (W.phase === 'meet') return 'table';
  let v = ''; try { v = localStorage.getItem(tvKey()) || ''; } catch (_) {}   // the owner's own choice wins
  return v === 'chart' || v === 'table' ? v : (W.phase === 'design' && !isPhone() ? 'chart' : 'table');
}
function teamView(v){ if (W.phase !== 'meet') { try { localStorage.setItem(tvKey(), v); } catch (_) {} } applyTeamView(v); }
function applyTeamView(v){
  v = v || viewFor(); const was = W.teamView; W.teamView = v;
  document.querySelectorAll('#team-view button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  $('#team-table').classList.toggle('hide', v === 'chart'); $('#canvas').classList.toggle('hide', v !== 'chart'); $('#canvas-tools').classList.toggle('hide', v !== 'chart');
  if (v === 'chart' && was !== 'chart') requestAnimationFrame(() => { if (W.phase !== 'meet') layoutAll(false); });
  if ($('#ph-k')) renderHead();
  fitTeam();
}
// the table uses the room it has. A big team: one line per agent. Fewer agents: each agent's goal, then its goal on two
// lines with its tools and standing orders. In the Overview, a small team in a tall panel is a list and its detail: the
// rows with their goals, the selected agent's brief under them (never a half-empty panel)
function fitTeam(){
  cancelAnimationFrame(W.fitRaf);
  W.fitRaf = requestAnimationFrame(() => {
    const p = $('#team-panel'); let d = 0, below = false;
    if (!isPhone() && W.phase !== 'meet' && W.teamView === 'table' && p.clientHeight > 0) {
      const ft = $('#team-foot'), avail = p.clientHeight - p.querySelector('.ph').offsetHeight - (ft.offsetHeight || 0) - 2;
      const rows = W.agents.size + 1, room = avail - 34;
      if (p.parentElement === $('#pane-overview') && W.agents.size && room - rows * 60 >= 200) { d = 1; below = true; }
      else d = room >= rows * 150 ? 3 : room >= rows * 104 ? 2 : room >= rows * 62 ? 1 : 0;
    }
    p.classList.toggle('d1', d === 1); p.classList.toggle('d2', d >= 2); p.classList.toggle('d3', d === 3);
    if (below !== !!W.below) { W.below = below; placeInsp(); }
  });
}
function renderRunPanel(){
  const body = $('#run-body'); const r = W.run;
  if (!r) { $('#run-id').textContent = ''; $('#run-meta').textContent = ''; body.innerHTML = W.deskId ? nextSteps() : '<div class="none">Runs start once the desk is built.</div>'; return; }
  const failed = !r.active && runFailed(r.status, r.summary);
  $('#run-id').textContent = r.id;
  $('#run-meta').innerHTML = r.active ? '<span class="tag running">running</span>' : failed ? '<span class="tag error">failed</span>' : `<span class="tag done">${esc(r.status || 'finished')}</span>`;
  const insts = [...r.inst.values()].filter(s => s.base !== 'atlas');
  const steps = insts.map((s, i) => { const cls = s.status === 'done' ? 'ok' : (s.status === 'error' ? 'err' : (['writing', 'thinking'].includes(s.status) ? 'run' : '')); const name = agentName(s.base);
    const sub = s.status === 'done' ? (s.turns ? plural(s.turns, 'turn') : 'done') : (s.status === 'writing' ? (s.turns ? `writing · turn ${s.turns}` : 'writing') : (s.status === 'thinking' ? (s.turns ? `turn ${s.turns}` : 'briefed') : (s.status === 'error' ? 'error' : (r.active ? 'queued' : 'did not finish'))));
    return `<div class="step ${cls}"><span class="sno">${cls === 'ok' ? '<svg class="i i12"><use href="#i-check"/></svg>' : i + 1}</span><span class="nm">${esc(name)}${s.ghost ? ` <span>${esc(s.inst)}</span>` : ''}<span>${esc(sub)}</span></span><span class="tm" title="${esc(s.assignment || '')}">${esc((s.assignment || '').slice(0, 48))}</span></div>`; });
  const lv = insts.filter(s => s.status === 'writing' && s.text).pop() || insts.filter(s => s.text).pop();
  const err = failed ? `<div class="run-err"><b>Run failed.</b> ${esc(String(r.summary || 'The run stopped with an error.').split('\n\n---\n')[0].replace(/^ERROR from [^:]+:\s*/, '').slice(0, 400))}</div>` : '';
  body.innerHTML = `<div class="run-task"><span class="gk">Job</span>${esc(r.task || '')}</div>${err}<div class="steps">${steps.join('') || (r.active ? `<div class="step run"><span class="sno">1</span><span class="nm">Atlas<span>briefing the team</span></span><span class="tm"></span></div>` : (failed ? '' : `<div class="step"><span class="sno">–</span><span class="nm">Atlas<span>no specialist was called</span></span><span class="tm"></span></div>`))}</div>`
    + (lv && (r.active || failed || !/---/.test(r.summary || '')) ? `<div class="out-box" id="run-live" data-inst="${esc(lv.inst)}"><span class="who">${esc(agentName(lv.base))}</span><span class="tx">${esc(lv.text.slice(-360))}</span>${r.active ? '<span class="caret"></span>' : ''}</div>` : '')
    + `<div class="rmeta" id="run-rmeta"></div>`;
  // the desk's own record of what the run left behind (a failed run keeps it too when something was queued or saved)
  const ver = !r.active && /\n---\n/.test(r.summary || '') ? String(r.summary).split('---').pop().trim().replace(/^Verified by the desk \(not model-claimed\):\s*/i, '') : '';
  if (ver) body.insertAdjacentHTML('beforeend', `<div class="run-sum${failed ? ' err' : ''}"><span class="gk">Verified by the desk, not claimed by the model</span>${esc(ver)} <button class="link" onclick="showTab('runs');openRun('${esc(r.id)}')">Read the ${failed ? 'error' : 'result'} ›</button></div>`);
  renderRunMeta();
}
// a desk with no run yet: what to do next, each with its button (nothing here is a number or a claim)
function nextSteps(){
  const wf = W.workflows || [];
  const rows = [
    ['i-plus', 'Give the team its first job', 'A reply to an enquiry, a report, a list to chase. Every agent\'s progress shows here as it happens.', '<button class="btn sm p" onclick="newJob()">New job</button>'],
    W.cams.size ? null : ['i-cam', 'Add a camera', 'RTSP stream, recording, webcam, snapshot URL or sample footage. Each camera keeps a written journal.', '<button class="btn sm" onclick="openAttach(null)">Add camera</button>'],
    ['i-inbox', 'Connect an app', 'Email, WhatsApp, SMS or a calendar, so approved messages can go out.', '<button class="btn sm" onclick="goDesk(\'integrations\')">Integrations</button>'],
    wf.length ? ['i-flow', 'Run a workflow', `${plural(wf.length, 'fixed hand-off')} between the agents: ${wf.map(w => w.name).join(', ')}.`, '<button class="btn sm" onclick="showTab(\'runs\')">Workflows</button>'] : null,
  ].filter(Boolean);
  return `<div class="nsteps"><div class="gk">No run yet · next steps</div>${rows.map(([ic, t, d, a]) => `<div class="nstep"><svg class="i"><use href="#${ic}"/></svg><div><b>${esc(t)}</b><small>${esc(d)}</small></div>${a}</div>`).join('')}</div>`;
}
async function goDesk(hash){ if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}}); location.href = '/desk#' + hash; }   // Home, on this desk
function renderRunLive(s){ const box = $('#run-live'); if (!box || box.dataset.inst !== s.inst) { if (!W.liveRaf) W.liveRaf = requestAnimationFrame(() => { W.liveRaf = 0; renderRunPanel(); }); return; } box.querySelector('.tx').textContent = s.text.slice(-360); }
function renderRunMeta(){
  const r = W.run; const el = $('#run-rmeta'); if (!r) return;
  const secs = Math.max(0, Math.round(((r.ended || Date.now()) - r.started) / 1000));
  if (el) el.innerHTML = `<span>Started <b>${esc(fmtTs(r.started / 1000, true))}</b> UTC</span><span>${r.active ? 'Elapsed' : 'Took'} <b>${fmtDur(secs)}</b></span><span>Tokens <b>${(r.tin || 0).toLocaleString()}</b> in · <b>${(r.tout || 0).toLocaleString()}</b> out</span>`;
  $('#sb-run-t').textContent = r.active ? fmtDur(secs) : '';
}
setInterval(() => { if (W.run && W.run.active) renderRunMeta(); }, 1000);
async function cancelRun(){ if (!W.run || !W.run.active) return; const r = await api(`/runs/${encodeURIComponent(W.run.id)}/cancel`, {method: 'POST', body: {}}); toast(r && !r.error ? 'Stopping the run' : ((r && r.error) || 'could not stop')); }
function runState(r){ if (r.active) return ['running', 'run', 'd-run']; if (runFailed(r.status, r.summary)) return ['failed', 'err', 'd-err']; return [r.status || 'done', r.status === 'done' ? 'ok' : '', r.status === 'done' ? 'd-ok' : 'd-q']; }
async function loadRuns(){
  if (!W.deskId) return;
  const rows = await api('/runs'); if (!Array.isArray(rows)) return;
  W.runs = rows;
  $('#runs-meta').textContent = rows.length ? `${plural(rows.length, 'run')}${rows.length >= 60 ? ' (newest 60)' : ''} · UTC` : '';
  $('#runs-list').innerHTML = rows.length ? rows.slice(0, 60).map(r => { const [lbl, cls, dot] = runState(r);
    return `<div class="rrow${W.openRunId === r.id ? ' sel' : ''}" onclick="openRun('${esc(r.id)}')" title="${esc(r.task || '')}"><i class="dot ${dot}"></i><span class="nm">${esc(r.task || '')}</span><span class="stx ${cls}">${esc(lbl)}</span><span class="mono">${((r.tokens_in || 0) + (r.tokens_out || 0)).toLocaleString()} tok</span><span class="mono" title="${esc(fullTs(r.created))}">${esc(fmtTs(r.created))}</span></div>`; }).join('')
    : `<div class="none">No runs yet. Give the team a job above, or run a workflow.</div>`;
  renderHead();
}
function showSummary(id, status, text){
  const failed = runFailed(status, text);
  $('#sum-h').className = 'sum-h' + (failed ? ' err' : ''); $('#sum-h').querySelector('use').setAttribute('href', failed ? '#i-alert' : '#i-check');
  $('#sum-title').textContent = failed ? 'Run failed' : 'Run finished';
  $('#summary-text').innerHTML = md((failed ? String(text || '').replace(/^ERROR from ([^:]+):\s*/, '**The run stopped: $1 hit an error.** ') : (text || '')).trim() || '(no summary)');
  $('#summary').classList.remove('hide'); $('#summary-empty').classList.add('hide');
  $('#sum-meta').textContent = `${id} · ${failed ? 'failed' : (status || 'finished')}`;
}
async function openRun(id){
  const r = await api(`/runs/${encodeURIComponent(id)}`); if (!r || r.error) return toast((r && r.error) || 'run not found');
  W.openRunId = id; document.querySelectorAll('.rrow').forEach(x => x.classList.toggle('sel', x.getAttribute('onclick') === `openRun('${id}')`));
  const status = r.active ? 'running' : r.status;
  const text = (r.summary || '').trim() + (r.deliverables || []).filter(d => d.name !== 'SUMMARY.md').map(d => `\n\n### ${d.name}\n\n${d.content}`).join('');   // SUMMARY.md is the summary itself
  showSummary(id, status, text);
}
function renderWorkflows(){
  const wf = W.workflows || [];
  $('#wf-panel').classList.toggle('hide', !wf.length); $('#wf-n').textContent = wf.length;
  $('#wf-list').innerHTML = wf.map(w => `<div class="wfrow" id="wf-${esc(w.id)}"><div style="min-width:0"><b>${esc(w.name)}</b><small title="${esc(w.description || '')}">${esc((w.description || '').replace(/->/g, '→'))}</small></div><button class="btn sm" onclick="runWorkflowNow(${jsq(w.id)})"><svg class="i"><use href="#i-run"/></svg>Run</button></div>`).join('');
}
function runWorkflowNow(id){ const w = (W.workflows || []).find(x => x.id === id); if (!w) return; deploy(`Run the "${w.name}" workflow now.`, w.id); }
function showTab(name, focus){
  W.tab = name; $('#wk').dataset.tab = name;
  document.querySelectorAll('#tabs .tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  document.querySelectorAll('.tpane').forEach(p => p.classList.toggle('hide', p.id !== 'pane-' + name));
  const tp = $('#team-panel'); if (name === 'team') $('#team-big').appendChild(tp); else if (tp.parentElement !== $('#pane-overview')) $('#pane-overview').insertBefore(tp, $('#run-panel'));
  const cams = $('#cams'); cams.classList.toggle('big', name === 'cameras'); cams.classList.toggle('strip', name !== 'cameras'); if (name === 'cameras') $('#cam-big').appendChild(cams); else if (cams.parentElement !== $('#camrow')) $('#camrow').insertBefore(cams, $('#cam-side'));
  fitCams(); fitTeam();
  const ap = $('#ap-panel'); if (name === 'approvals') $('#ap-big').appendChild(ap); else if (ap.parentElement !== $('#right')) $('#right').insertBefore(ap, $('#right').firstChild);
  placeInsp();
  renderApprovals();
  if (name === 'cameras') { loadJournal(); if (focus === 'journal') setTimeout(() => $('#journal-panel').scrollIntoView({behavior: 'smooth', block: 'start'}), 80); }
  if (name === 'runs') loadRuns(); if (name === 'approvals') loadApprovals();
  if (name === 'team' && W.teamView === 'chart') requestAnimationFrame(() => { if (W.phase !== 'meet') layoutAll(false); });
  if (isPhone()) { const t = $('#tabs'); if (t.getBoundingClientRect().top < 0) t.scrollIntoView({block: 'start'}); }
}
function newJob(){ showTab('runs'); setTimeout(() => $('#job').focus({preventScroll: true}), 60); }
function clearFeed(){ $('#feed').innerHTML = ''; }
function exFilter(f){ W.exf = f; document.querySelectorAll('#ex-seg button').forEach(b => b.classList.toggle('on', b.dataset.f === f)); renderExplorer(); }
function exToggle(key){ const c = W.exClosed || (W.exClosed = new Set()); c.has(key) ? c.delete(key) : c.add(key); try { localStorage.setItem('ws_ex_closed', JSON.stringify([...c])); } catch (_) {} renderExplorer(); }
function renderExplorer(){
  if (!$('#tree')) return;
  const q = ($('#ex-filter').value || '').trim().toLowerCase(); const f = W.exf || 'all';
  const match = s => !q || String(s).toLowerCase().includes(q);
  const closed = W.exClosed || new Set(); const out = [];
  const grp = (key, icon, name, n, meta, metaCls, rows) => { if (!rows.length) return; const shut = closed.has(key) && !q;
    out.push(`<button class="grp${shut ? ' closed' : ''}" onclick="exToggle('${key}')" title="${shut ? 'Show' : 'Hide'} ${esc(name.toLowerCase())}"><svg class="i i12 ch"><use href="#i-down"/></svg><svg class="i i14"><use href="#${icon}"/></svg>${name}<span class="n">${n}</span><span class="m ${metaCls || ''}">${esc(meta || '')}</span></button>${shut ? '' : rows.join('')}`); };
  const row = (icon, name, meta, metaCls, onclick, sel, extra, title) => `<div class="row ${sel ? 'sel' : ''}" onclick="${onclick}" title="${esc(title || name)}"><svg class="i"><use href="#${icon}"/></svg><span class="nm">${esc(name)}</span>${extra || ''}<span class="m ${metaCls || ''}">${esc(meta || '')}</span></div>`;
  const ids = W.agents.size ? ['atlas', ...W.order] : [];
  const arows = []; let working = 0, errors = 0;
  for (const id of ids) { const a = id === 'atlas' ? {name: 'Atlas'} : W.agents.get(id); if (!a) continue; const st = agentState(id), g = agentGaps(id), needs = st.cls === 'err' || g.length > 0;
    if (st.cls === 'run') working++; if (needs) errors++;
    if (!match(a.name) || (f === 'active' && st.cls !== 'run') || (f === 'attention' && !needs)) continue;
    const meta = st.cls === 'run' || st.cls === 'err' || !g.length ? st.label.toLowerCase() : `${st.label.toLowerCase()} · ${plural(g.length, 'gap')}`;
    arows.push(row('i-agent', a.name, meta, st.cls === 'run' ? 'run' : st.cls === 'err' ? 'err' : g.length ? 'warn' : '', `inspect(${jsq(id)})`, W.sel === id, id === 'atlas' ? '<span class="tag">lead</span>' : '', g.length ? g.join('; ') : a.name)); }
  grp('agents', 'i-team', 'Agents', ids.length, working ? `${working} working` : (errors ? plural(errors, 'agent') + ' to check' : (W.phase === 'run' ? 'ready' : (ids.length ? 'draft' : ''))), working ? 'run' : errors ? 'warn' : '', arows);
  const crows = []; let liveN = 0, attn = 0;
  for (const [name, k] of W.cams) { const built = !!k.id, live = k.st === 'live'; if (live) liveN++; const needs = built && (k.problem || !live); if (needs) attn++;
    if (!match(name) || (f === 'active' && !live) || (f === 'attention' && !needs)) continue;
    const meta = !built ? 'draft' : k.problem ? 'fix feed' : live ? (countsText(k.counts) || 'live') : k.st === 'waiting' ? 'no frame' : 'not watching';
    crows.push(row('i-cam', name, meta, !built ? '' : k.problem ? 'warn' : live ? 'ok' : k.st === 'waiting' ? 'warn' : '', `camClick(${jsq(name)})`, false, '', k.problem || meta)); }
  grp('cameras', 'i-cam', 'Cameras', W.cams.size, !W.cams.size ? '' : !W.deskId ? 'planned' : `${liveN} of ${W.cams.size} live`, liveN < W.cams.size && W.deskId ? 'warn' : '', crows);
  if (f === 'all') { const wrows = (W.workflows || []).filter(w => match(w.name)).map(w => row('i-flow', w.name, 'runs', '', `showTab('runs');document.getElementById('wf-${esc(w.id)}')&&document.getElementById('wf-${esc(w.id)}').scrollIntoView({block:'nearest'})`, false, '', w.description)); grp('workflows', 'i-flow', 'Workflows', (W.workflows || []).length, '', '', wrows); }
  if (W.apCount && f !== 'active' && match('approvals waiting')) grp('approvals', 'i-inbox', 'Approvals', W.apCount, 'oldest ' + (W.apOldest || ''), 'warn', [row('i-inbox', `${plural(W.apCount, 'message')} waiting for you`, '', 'warn', "showTab('approvals')", false)]);
  const lastRun = (W.runs || [])[0];
  if (lastRun && f !== 'active' && runFailed(lastRun.status, lastRun.summary) && match('last run failed ' + (lastRun.task || ''))) grp('runs', 'i-activity', 'Runs', 1, 'last run failed', 'warn', [row('i-activity', lastRun.task || lastRun.id, 'failed', 'err', `showTab('runs');openRun('${esc(lastRun.id)}')`, false)]);
  $('#tree').innerHTML = out.join('') || `<div class="none">${q ? 'Nothing matches.' : f === 'attention' ? 'Nothing needs attention.' : f === 'active' ? 'Nothing is running.' : 'No objects yet.'}</div>`;
  const cams = [...W.cams.values()]; const fixes = cams.filter(c => c.problem).length;
  $('#h-journal').innerHTML = cams.length ? `<i class="dot ${liveN === cams.length ? 'd-ok' : 'd-warn'}"></i><span title="${fixes ? plural(fixes, 'camera') + ' need the stream address re-entered' : ''}">${liveN} of ${cams.length} live${fixes ? ' · ' + fixes + ' to fix' : ''}</span>` : '<span>none</span>';
}
async function pollStats(){
  if (!W.deskId) return;
  const s = await api('/stats'); if (!s || s.needs_desk || s.error) return;
  W.activeRuns = s.active_runs || 0; W.cost = s.cost || null;
  if (W.apCount != null && s.pending !== W.apCount) loadApprovals();
  renderKpis(); renderRunPill(); if (!W.cams.size) stamp();
}
async function pollHealth(){
  if (!W.deskId) return;
  const h = await api('/health/full'); if (!h || h.error) return;
  const alerts = h.alerts || []; const top = alerts.find(a => a.level === 'critical') || alerts[0];
  const b = $('#banner');
  if (top) { b.classList.remove('hide'); b.classList.toggle('bad', top.level === 'critical'); $('#banner-msg').textContent = String(top.text || '').replace(/(\d+)h old/, (m, hh) => +hh >= 48 ? Math.round(+hh / 24) + ' d old' : m); }
  else b.classList.add('hide');
}
setInterval(() => { if (W.deskId && !document.hidden) { pollHealth(); loadRuns(); loadGaps(); } }, 60000);
// what stops an agent doing part of its job (api /agents/links: the same list Home's "Needs you" shows)
async function loadGaps(){
  if (!W.deskId) return;
  const r = await api('/agents/links'); if (!r || !r.agents) return;
  W.gaps = new Map(Object.entries(r.agents).map(([id, e]) => [id, (e && e.gaps) || []]).filter(([, g]) => g.length));
  renderAll();
}
const gapCount = () => [...(W.gaps || new Map()).values()].reduce((m, g) => m + g.length, 0);

/* ------------------------------------------------------------------ layout by the workspace's own width (viewport minus the rail) */
const SIZES = [['xl', 1560], ['l', 1280], ['lm', 1040], ['m', 860], ['s', 0]];
function isPhone(){ return $('#wk').dataset.w === 's'; }
function docked(){ const s = $('#wk').dataset.w, ph = W.phase; if (ph === 'meet') return true; if (s === 'xl' || s === 'l' || s === 'lm') return true; return s === 'm' && ph === 'design'; }
function placePanels(){
  const ws = $('#ws'), d = docked(), s = $('#wk').dataset.w;
  ws.classList.toggle('dock', d);
  ws.classList.remove('exdock');                                         // the explorer is a drawer at every width: the rail is the left column
  const act = $('#act-panel'), short = $('#wk').clientHeight < 860;     // a short screen keeps the right stack for approvals and Atlas (Activity goes under Runs)
  if (d && !short) { if (act.parentElement !== $('#right')) $('#right').insertBefore(act, $('#atlas-panel')); }
  else if (act.parentElement !== $('#pane-runs')) $('#pane-runs').appendChild(act);
  if (d) ws.classList.remove('atlas-open');
}
function applySize(){
  const wk = $('#wk'); const w = wk.clientWidth || innerWidth;
  const s = SIZES.find(([, min]) => w >= min)[0];
  if (wk.dataset.w !== s) { const was = wk.dataset.w; wk.dataset.w = s; document.body.dataset.w = s; if (W.tab === 'team') placeInsp();
    if (was && (was === 's') !== (s === 's') && W.teamView && W.teamView !== viewFor()) applyTeamView(); }   // the default view follows the layout until the owner picks one
  wk.dataset.sw = w >= 720 ? 'w' : 'n';                                   // one column: five stat tiles across from 720 px, 3 + 2 below
  placePanels(); fitCams();
}
// one column: whole rows of camera tiles, the Add camera tile filling the last one (2-4 columns, the fewest blank cells)
function fitCams(){
  const cams = $('#cams'); if (!cams) return;
  if (!isPhone() || !cams.classList.contains('strip')) { cams.style.removeProperty('--scols'); return; }
  const w = $('#camstrip').clientWidth || innerWidth, total = W.cams.size + 1, max = Math.max(2, Math.min(4, Math.floor(w / 150)));
  let best = max, blank = 99;
  for (let c = max; c >= 2; c--) { const b = (c - total % c) % c; if (b < blank) { best = c; blank = b; } }
  cams.style.setProperty('--scols', best);
}
function openAtlas(){
  const ws = $('#ws');
  if (ws.classList.contains('dock')) { const p = $('#atlas-panel'); p.classList.remove('flash'); void p.offsetWidth; p.classList.add('flash'); }
  else ws.classList.add('atlas-open');
  setTimeout(() => { $('#msgs').scrollTop = 1e9; $('#say').focus({preventScroll: true}); }, 60);
}
function closeAtlas(){ $('#ws').classList.remove('atlas-open'); }
// one column: a tap on an explorer row opens its object, and the drawer gets out of the way
document.getElementById('tree').addEventListener('click', e => { if (isPhone() && e.target.closest('.row')) toggleExplorer(false); });
function toggleExplorer(on){ const ws = $('#ws'); if (ws.classList.contains('exdock')) return; const v = on ?? !ws.classList.contains('ex-open'); ws.classList.toggle('ex-open', v); if (v) renderExplorer(); }

/* ------------------------------------------------------------------ the app frame (shell.js) talks to this page through window.shellPage */
window.shellPage = {
  ask(mode, text){ if (W.phase === 'run' && mode) setMode(mode); openAtlas(); if (text) $('#say').value = text; return true; },   // the rail's Ask Atlas opens THIS conversation
  replaces: ['agents', 'cameras', 'add-camera'],
  commands(){
    const c = [], add = (label, ic, run, hint) => c.push({g: 'This workspace', label, ic, run, hint});
    if (W.phase === 'run') add('New job', 'plus', newJob, 'N');
    if (W.bp && !W.deskId && (W.bp.agents || []).length) add('Build desk', 'spark', buildDesk);
    add('Add camera', 'camera', () => openAttach(null));
    if (W.deskId) add('Approvals', 'check', toggleApprovals, W.apCount ? W.apCount + ' waiting' : 'none waiting');
    const tabs = W.phase === 'run' ? [['overview', 'Overview'], ['team', 'Team'], ['cameras', 'Cameras'], ['runs', 'Runs'], ['approvals', 'Approvals']] : W.phase === 'design' ? [['overview', 'Overview'], ['team', 'Team'], ['cameras', 'Cameras']] : [];
    tabs.forEach(([t, l]) => add('Show ' + l, 'list', () => showTab(t), 'tab'));
    if (W.deskId && W.cams.size) add('Read the camera journal', 'film', () => showTab('cameras', 'journal'));
    if (W.deskId) add('Explorer: agents, cameras, workflows', 'grid', () => toggleExplorer(true));
    add('How Atlas works', 'doc', openGuide); if (W.agents.size) add('Reset the org chart layout', 'grid', resetLayout);
    if (W.sid && !W.deskId) add(W.tier === 'free' ? 'Switch to paid models' : 'Switch to free models', 'gear', toggleTier);
    (W.workflows || []).forEach(w => c.push({g: 'Workflows', label: w.name, hint: 'run now', ic: 'play', run: () => runWorkflowNow(w.id)}));
    if (W.agents.size) c.push({g: 'Agents', label: 'Atlas', hint: 'orchestrator', ic: 'team', run: () => inspect('atlas')});
    W.agents.forEach(a => c.push({g: 'Agents', label: a.name, hint: a.role, ic: 'team', run: () => inspect(a.id)}));
    W.cams.forEach(k => c.push({g: 'Cameras', label: k.name, hint: !k.id ? 'draft' : k.st === 'live' ? 'live view' : k.problem ? 'fix the feed' : 'feed settings', ic: 'camera', run: () => camClick(k.name)}));
    return c;
  },
};
