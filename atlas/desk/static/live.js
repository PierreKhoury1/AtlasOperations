/* Live desk: mission control over one run. Uses the page's globals ($, api, esc, hms, when, jsa, CFG, colors,
   agentName, page, go, toast). Same run events as before (/api/runs/<id> history + /api/stream live). */
let liveWant = null, liveId = null, liveES = null, liveState = null, liveTimer = null;
const LV = { filter: 'all', stick: true, maxSay: 6000, lastTick: 0 };

function liveRefreshList(){
  api('/runs').then(R => {
    if (!R) return;
    const sel = $('#live-run'); if (!sel) return;
    const opts = R.slice(0, 30).map(r => `<option value="${r.id}">${r.active ? '● ' : ''}${esc(r.task.replace(/^New inbound lead[^\n]*\n(Name: )?/, '')).split('\n')[0].slice(0, 64)} · ${when(r.created)} · ${r.active ? 'running' : r.status}</option>`).join('');
    if (sel.innerHTML !== opts) sel.innerHTML = opts;
    const active = R.filter(r => r.active);
    let want = liveWant && R.find(r => r.id === liveWant) ? liveWant : null;
    if (!want && !liveId && (active[0] || R[0])) want = (active[0] || R[0]).id;
    if (!want && liveId && liveState && !liveState.active && active[0]) want = active[0].id;
    if (want && want !== liveId) { liveWant = null; pickLive(want); }
    if (liveId) sel.value = liveId;
  });
}

async function pickLive(id){
  if (!id) return;
  liveId = id; if ($('#live-run')) $('#live-run').value = id;
  const r = await api('/runs/' + id); if (!r || r.error) return;
  liveState = { id, active: r.active, t0: r.created, tEnd: r.ended || null, task: r.task || '', agents: {}, n: 0, tin: r.tokens_in, tout: r.tokens_out,
                feed: [], ticks: [], out: { approvals: [], cases: {}, records: {}, deliverables: [], policy: [], sent: 0, tools: 0, handoffs: 0 }, status: r.status };
  if (r.active) { const L = await api('/live'); const me = L && L.runs.find(x => x.id === id); if (me) { liveState.tin = me.tokens_in; liveState.tout = me.tokens_out; } }
  LV.stick = true; liveState.raw = r.events || []; LV.replaying = false;
  lvBuildTeam(); LVG.mount(); LVG.build(CFG.agents); const _lg = document.querySelector('.lv-glegend i[data-atlas]'); if (_lg) _lg.style.background = colors.atlas || '#ea580c';
  $('#lv-feed').innerHTML = '';
  (r.events || []).forEach(e => lvApply(e, false));
  lvSyncGraphCounts(); lvRenderAll(); LVG.redraw();
  $('#lv-replay').style.display = r.active ? 'none' : '';
  if (liveES) { liveES.close(); liveES = null; }
  if (r.active) {
    liveES = new EventSource('/api/stream?since=now&run=' + encodeURIComponent(id));
    liveES.onmessage = ev => { const e = JSON.parse(ev.data); lvApply(e, true); lvSchedule(); };
    liveES.onerror = () => {};
  }
  clearInterval(liveTimer);
  liveTimer = setInterval(() => {
    if (page !== 'live' || !liveState) return;
    const S = liveState, t = S.active ? (Date.now()/1000 - S.t0) : ((S.tEnd || Date.now()/1000) - S.t0);
    $('#lv-elapsed').textContent = lvDur(Math.max(0, t));
    if (S.active) lvTimeline();
  }, 500);
}

/* ---------------- state */
const lvKey = e => (e.data && e.data.inst) || e.agent;
function lvAgent(key){
  const S = liveState, base = String(key).split('#')[0], n = String(key).includes('#') ? key.split('#')[1] : '';
  if (!S.agents[key]) {
    const a = CFG.agents.find(x => x.id === base);
    S.agents[key] = { id: key, base, name: (a ? a.name : base) + (n ? ' #' + n : ''), role: a ? a.role : '', color: colors[base] || (a && a.color) || '#7c3aed',
                      status: 'idle', assignment: '', now: '', turns: 0, tools: 0, chars: 0, done: false, spark: [], say: null, think: '', model: '' };
    if (!document.getElementById('lva-' + lvId(key))) lvAddAgentNode(S.agents[key]);
  }
  return S.agents[key];
}
const lvId = k => String(k).replace(/[^a-z0-9_]/gi, '-');

function lvParseTool(text){
  // "record_find('Choumert') → 3" | "case_list → case #5 ..." | "delegate → repairs_desk (…)" | "list_files()"
  const m = text.match(/^([a-z_][\w]*)\s*(\((.*?)\))?\s*(?:→|->)?\s*([\s\S]*)$/i);
  if (!m) return { name: text.split(/[( →:]/)[0], args: '', result: '' };
  return { name: m[1], args: (m[3] || '').trim(), result: (m[4] || '').trim() };
}
const lvMd = t => esc(t).replace(/\*\*([^*]{1,200}?)\*\*/g, '<b>$1</b>').replace(/`([^`]{1,200})`/g, '<code>$1</code>');
const lvStrip = t => String(t || '').replace(/<atlas>[\s\S]*?<\/atlas>/g, '').replace(/<atlas>[\s\S]*$/, '').replace(/\n{3,}/g, '\n\n').trim();

function lvApply(e, live){
  const S = liveState; if (!S) return; S.n++;
  if (typeof e.data === 'string') { try { e.data = JSON.parse(e.data || '{}'); } catch (_) { e.data = {}; } }
  e.data = e.data || {};
  const k = e.kind, sys = e.agent === 'system' || e.agent === 'owner';
  if (k === 'usage') { S.tin = e.data.tokens_in; S.tout = e.data.tokens_out; return; }
  const a = sys ? null : lvAgent(lvKey(e));
  const tick = (cls) => S.ticks.push({ ts: e.ts, key: a ? a.id : 'system', color: a ? a.color : '#9aa3b2', cls, i: S.feed.length });
  if (k === 'agent_start' && a) {
    a.status = 'thinking'; a.done = false; a.turns = 0; a.say = null; a.think = '';
    if (e.data.assignment) a.assignment = e.data.assignment;
    if (e.data.model) a.model = e.data.model;
    if (a.base !== 'atlas') { S.out.handoffs++; lvPush({ type: 'hand', ts: e.ts, from: e.data.parent || 'atlas', to: a, text: e.data.assignment || e.text }); tick('hand'); lvHandoffFlash(a); if (live) LVG.on('handoff', a); }
    else { lvPush({ type: 'start', ts: e.ts, a, text: e.data.assignment || S.task }); if (live) LVG.on('start', a); }
    return;
  }
  if (k === 'token' && a) {
    if (e.data.thinking) { a.think = (a.think + e.text).slice(-1500); a.status = 'thinking'; }
    else { if (!a.say || a.say.closed) { a.say = { type: 'say', ts: e.ts, a, text: '', live: true }; lvPush(a.say); } a.say.text += e.text; a.chars += e.text.length; a.status = 'writing'; if (a.say.text.length > LV.maxSay) a.say.text = a.say.text.slice(-LV.maxSay); lvUpdateSay(a.say); }
    return;
  }
  if (k === 'log' && a) {
    a.turns++; a.status = 'thinking'; a.think = '';
    const txt = lvStrip(e.text);
    if (a.say && !a.say.closed) { a.say.text = e.text; a.say.closed = true; a.say.live = false; lvUpdateSay(a.say); }
    else if (txt) lvPush({ type: 'say', ts: e.ts, a, text: e.text, closed: true });
    else if (a.say) { a.say.closed = true; }
    a.say = a.say && a.say.closed ? null : a.say;
    return;
  }
  if (k === 'log' && sys) { if (!/^run \S+\s+mode=/.test(e.text)) lvPush({ type: 'sys', ts: e.ts, text: e.text }); return; }
  if (k === 'tool' && a) {
    const p = lvParseTool(e.text); a.tools++; S.out.tools++; a.status = 'tool'; a.now = p.name + (p.args ? '(' + p.args.slice(0, 40) + ')' : '');
    a.spark.push(p.name === 'queue_action' ? 'a' : 't'); if (a.spark.length > 40) a.spark.shift();
    if (a.say && !a.say.closed) { a.say.closed = true; a.say.live = false; lvUpdateSay(a.say); a.say = null; }
    const last = S.feed[S.feed.length - 1];
    if (last && last.type === 'tools' && last.a === a && e.ts - last.ts < 90) { last.rows.push(p); lvUpdateTools(last); }
    else lvPush({ type: 'tools', ts: e.ts, a, rows: [p] });
    tick('tool');
    lvOutcomeFromTool(a, p); if (live) LVG.on('tool', a, p.name);
    if (p.name === 'finish') a.status = 'finishing';
    return;
  }
  if (k === 'approval') {
    const item = { ts: e.ts, kind: e.data.action_kind || 'action', to: e.data.to || '', text: e.text.replace(/^[a-z_]+ → [^:]*: ?/i, ''), id: e.data.action_id, by: a ? a.name : '' };
    S.out.approvals.push(item); if (a) a.spark.push('a');
    lvPush({ type: 'appr', ts: e.ts, a, item }); tick('appr'); if (live) LVG.on('approval', a, item.kind); return;
  }
  if (k === 'policy') { const v = e.data.violations || [e.text]; S.out.policy.push({ ts: e.ts, by: a ? a.name : '', text: e.text, v }); if (a) a.spark.push('p'); lvPush({ type: 'pol', ts: e.ts, a, text: e.text, v }); tick('pol'); if (live) LVG.on('policy', a); return; }
  if (k === 'deliverable') { const f = String(e.text).split(/[\\/]/).pop(); S.out.deliverables.push(f); lvPush({ type: 'deliv', ts: e.ts, files: [f] }); tick('deliv'); return; }
  if (k === 'sent') { S.out.sent++; lvPush({ type: 'sys', ts: e.ts, text: '📤 ' + e.text }); return; }
  if (k === 'agent_end' && a) { a.status = 'done'; a.done = true; a.now = ''; if (a.say && !a.say.closed) { a.say.closed = true; a.say.live = false; lvUpdateSay(a.say); a.say = null; } if (a.base !== 'atlas') { lvPush({ type: 'back', ts: e.ts, a, text: e.text.replace(/^.*? done — /, '') }); if (live) LVG.on('back', a); } return; }
  if (k === 'error') { if (a) a.status = 'error'; lvPush({ type: 'err', ts: e.ts, a, text: e.text }); tick('pol'); if (live) LVG.on('error', a); return; }
  if (k === 'done') {
    S.active = false; S.tEnd = e.ts; S.status = (e.data && e.data.status) || 'done';
    Object.values(S.agents).forEach(x => { if (!x.done && x.status !== 'idle') { x.status = 'done'; x.done = true; x.now = ''; } });
    const lastSay = [...S.feed].reverse().find(x => x.type === 'say'); if (lastSay && lastSay.el && lvStrip(lastSay.text).slice(0, 80) === String(e.text).slice(0, 80)) lastSay.el.style.display = 'none';
    lvPush({ type: 'end', ts: e.ts, text: e.text, status: S.status }); if (live) LVG.on('done'); return;
  }
}

function lvOutcomeFromTool(a, p){
  const O = liveState.out;
  if (/^case_(open|update)$/.test(p.name)) {
    const r = p.result || '', m = r.match(/case #(\d+)\s*(.*)/);
    if (m && !/^ERROR/i.test(r)) { const id = m[1]; let what = m[2].replace(/^\(|\)$/g, '').replace(/^updated:\s*/, '').replace(/ - the desk will work it| and bound it to this run.*$/, '').trim();
      O.cases[id] = { id, title: what.slice(0, 70) || (p.name === 'case_open' ? 'opened' : 'updated'), what: p.name === 'case_open' ? 'opened' : 'updated', by: a.name }; }
  }
  if (p.name === 'record_save') { const m = (p.result || '').match(/#(\d+)\s*(.*)/); const key = m ? m[1] : (p.args || '').slice(0, 40); O.records[key] = { title: (m ? m[2] : p.args).slice(0, 60) || ('record ' + key), by: a.name }; }
  if (p.name === 'crm_update') { O.records['crm:' + (p.args || '').slice(0, 40)] = { title: 'CRM: ' + (p.args || '').slice(0, 56), by: a.name }; }
}

/* ---------------- feed DOM */
function lvPush(item){ const S = liveState; item.i = S.feed.length; S.feed.push(item); const el = lvRenderItem(item); if (el) { item.el = el; $('#lv-feed').appendChild(el); lvApplyFilter(el, item); lvStickScroll(); } }
function lvShow(type){ const f = LV.filter; return f === 'all' || (f === 'tools' && type === 'tools') || (f === 'hand' && (type === 'hand' || type === 'back' || type === 'start')) || (f === 'appr' && type === 'appr') || (f === 'pol' && (type === 'pol' || type === 'err')) || (f === 'say' && type === 'say'); }
function lvApplyFilter(el, item){ el.style.display = lvShow(item.type) ? '' : 'none'; }
function lvSetFilter(f){ LV.filter = f; document.querySelectorAll('.lv-filters button[data-f]').forEach(b => b.classList.toggle('on', b.dataset.f === f)); (liveState ? liveState.feed : []).forEach(it => it.el && lvApplyFilter(it.el, it)); lvStickScroll(true); }
function lvStickScroll(force){ const bd = $('#lv-feedwrap'); if (!bd) return; if (LV.stick || force) bd.scrollTop = bd.scrollHeight; $('#lv-jump').classList.toggle('show', !LV.stick && !!(liveState && liveState.active)); }
function lvOnScroll(){ const bd = $('#lv-feedwrap'); LV.stick = bd.scrollHeight - bd.scrollTop - bd.clientHeight < 40; $('#lv-jump').classList.toggle('show', !LV.stick && !!(liveState && liveState.active)); }
function lvJump(){ LV.stick = true; lvStickScroll(true); }
const lvWho = (a, kind, ts) => `<div class="who"><b>${esc(a ? a.name : 'Atlas desk')}</b>${kind ? `<span class="k">${kind}</span>` : ''}<span class="t">${hms(ts)}</span></div>`;
function lvRenderItem(it){
  const d = document.createElement('div'); const c = it.a ? it.a.color : '';
  d.className = 'lv-ev ' + it.type; if (c) d.style.setProperty('--c', c); d.dataset.i = it.i;
  if (it.type === 'say') { d.innerHTML = lvWho(it.a, '', it.ts) + `<div class="tx"></div>`; lvUpdateSay(it, d); }
  else if (it.type === 'tools') { d.innerHTML = lvWho(it.a, 'tools', it.ts) + `<div class="rows"></div>`; lvUpdateTools(it, d); }
  else if (it.type === 'hand') { const from = it.from === 'atlas' ? 'Atlas' : agentName(String(it.from).split('#')[0]); d.innerHTML = lvWho(it.to, 'hand-off', it.ts) + `<div class="arrow"><span>${esc(from)}</span><i></i><span style="color:var(--c)">${esc(it.to.name)}</span></div><div class="asg">${esc(it.text || '')}</div>`; }
  else if (it.type === 'start') { d.className = 'lv-ev hand'; d.innerHTML = lvWho(it.a, 'job', it.ts) + `<div class="arrow"><span>You</span><i></i><span style="color:var(--c)">${esc(it.a.name)}</span></div><div class="asg">${esc(it.text || '')}</div>`; }
  else if (it.type === 'back') { d.innerHTML = lvWho(it.a, 'reports back', it.ts) + `<div class="tx">${lvMd(it.text)}</div>`; }
  else if (it.type === 'appr') { const x = it.item; d.innerHTML = lvWho(it.a, 'needs you', it.ts) + `<div class="sub">${esc(x.kind)} · ${esc(x.text)}</div><div class="to">to ${esc(x.to)} · held until you approve</div><div class="act"><button class="btn sm p" onclick="go('approvals')">Review ›</button></div>`; }
  else if (it.type === 'pol') { d.innerHTML = lvWho(it.a, 'policy blocked', it.ts) + `<div>${esc(it.text.split(':')[0])}</div><ul>${(it.v || []).map(v => `<li>${esc(v)}</li>`).join('')}</ul>`; }
  else if (it.type === 'deliv') { d.innerHTML = lvWho(null, 'deliverable', it.ts) + it.files.map(f => `<span class="file">${esc(f)}</span>`).join(''); }
  else if (it.type === 'end') { d.innerHTML = lvWho(null, it.status === 'done' ? 'finished' : it.status, it.ts) + `<div class="tx">${lvMd(it.text)}</div>`; }
  else if (it.type === 'err') { d.innerHTML = lvWho(it.a, 'error', it.ts) + `<div class="tx">${esc(it.text)}</div>`; }
  else if (it.type === 'sys') { d.className = 'lv-ev sys'; d.innerHTML = lvWho(null, '', it.ts) + `<div class="tx">${esc(it.text)}</div>`; }
  return d;
}
function lvUpdateSay(it, el){
  el = el || it.el; if (!el) return;
  const tx = el.querySelector('.tx'); const txt = lvStrip(it.text);
  const think = it.live && it.a.think ? `<span class="think">${esc(it.a.think.slice(-400))}</span>\n` : '';
  tx.innerHTML = think + lvMd(txt) + (it.live && liveState.active ? '<span class="caret"></span>' : '');
  if (!txt && !think) el.style.display = 'none'; else lvApplyFilter(el, it);
  if (it.live) tx.scrollTop = 1e9;
}
function lvUpdateTools(it, el){
  el = el || it.el; if (!el) return;
  el.querySelector('.rows').innerHTML = it.rows.map(p => {
    const r = p.result || '', zero = /^0$|^\(none\)|^none$|→ 0$/.test(r), ok = /^(saved|ok|queued|done|booked|opened|case #\d+|found)/i.test(r), bad = /^(ERROR|NOT ALLOWED|blocked)/i.test(r);
    return `<div class="row"><span class="fn">${esc(p.name)}</span><span class="ar" title="${esc(p.args)}">${esc(p.args)}</span><span class="rs ${bad ? 'bad' : zero ? 'zero' : ok ? 'ok' : ''}" title="${esc(r)}">${esc(r.slice(0, 120))}</span></div>`;
  }).join('');
}

/* ---------------- team rail */
function lvBuildTeam(){
  const g = $('#lv-team'); if (!g) return; const lead = CFG.agents.find(a => a.id === 'atlas'); const rest = CFG.agents.filter(a => a.id !== 'atlas');
  g.innerHTML = (lead ? lvAgentNode(lvAgent('atlas'), true) : '') + `<div class="lv-sub" id="lv-sub">${rest.map(a => lvAgentNode(lvAgent(a.id))).join('')}</div>`;
}
function lvAgentNode(a, lead){
  return `<div class="lv-agent ${lead ? 'lead' : ''}" id="lva-${lvId(a.id)}" style="--c:${a.color}">
    <div class="nm">${esc(a.name)}<span class="st" id="lvst-${lvId(a.id)}">idle</span></div>
    <div class="role">${esc(a.role)}</div>
    <div class="asg" id="lvasg-${lvId(a.id)}"></div>
    <div class="now" id="lvnow-${lvId(a.id)}"></div>
    <div class="spark" id="lvsp-${lvId(a.id)}"></div>
    <div class="meta" id="lvm-${lvId(a.id)}"></div></div>`;
}
function lvAddAgentNode(a){ const sub = $('#lv-sub'); if (!sub) { LVG.on('_add', a); return; } const t = document.createElement('div'); t.innerHTML = lvAgentNode(a); const el = t.firstElementChild; const sib = document.getElementById('lva-' + lvId(a.base)); if (sib && sib.parentElement === sub && sib.nextSibling) sub.insertBefore(el, sib.nextSibling); else sub.appendChild(el); }
function lvHandoffFlash(a){ const el = document.getElementById('lva-' + lvId(a.id)); if (!el) return; el.classList.remove('handoff'); void el.offsetWidth; el.classList.add('handoff'); setTimeout(() => el.classList.remove('handoff'), 1000); }
function lvRenderTeam(){
  const S = liveState; if (!$('#lv-team')) { LVG.redraw(); return; }
  Object.values(S.agents).forEach(a => {
    const el = document.getElementById('lva-' + lvId(a.id)); if (!el) return;
    el.classList.toggle('busy', !a.done && a.status !== 'idle'); el.classList.toggle('fin', a.done);
    $('#lvst-' + lvId(a.id)).textContent = a.status;
    const asg = $('#lvasg-' + lvId(a.id)); asg.textContent = a.base === 'atlas' ? '' : a.assignment; asg.title = a.assignment;
    $('#lvnow-' + lvId(a.id)).textContent = !a.done && a.status === 'tool' ? a.now : (a.status === 'writing' ? 'writing…' : a.status === 'thinking' && !a.done ? 'thinking…' : '');
    $('#lvsp-' + lvId(a.id)).innerHTML = a.spark.slice(-36).map(s => `<i class="${s}"></i>`).join('');
    $('#lvm-' + lvId(a.id)).innerHTML = (a.turns || a.tools) ? `<span>${a.turns} turn${a.turns === 1 ? '' : 's'}</span><span>${a.tools} tool call${a.tools === 1 ? '' : 's'}</span>${a.model ? `<span title="${esc(a.model)}">${esc(a.model.split('/').pop().slice(0, 18))}</span>` : ''}` : '';
  });
}

/* ---------------- outcomes */
function lvBump(id, v){ const el = $(id); if (!el) return; const s = String(v); if (el.textContent !== s) { el.textContent = s; el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); setTimeout(() => el.classList.remove('bump'), 600); } }
function lvRenderOut(){
  const S = liveState, O = S.out;
  $('#lv-task').innerHTML = `<b>The job</b>${esc(S.task.split('\n')[0].slice(0, 400))}`;
  lvBump('#lvk-appr', O.approvals.length); lvBump('#lvk-cases', Object.keys(O.cases).length); lvBump('#lvk-rec', Object.keys(O.records).length); lvBump('#lvk-tools', O.tools);
  const li = (tg, tx, by) => `<li><span class="tg">${esc(tg)}</span><span class="tx" title="${esc(tx)}">${esc(tx)}</span>${by ? `<span class="by">${esc(by)}</span>` : ''}</li>`;
  $('#lvo-appr').innerHTML = O.approvals.length ? O.approvals.slice(-6).reverse().map(x => li(x.kind, x.text, x.by)).join('') : '<div class="empty">Nothing queued yet. Outbound waits here for you.</div>';
  $('#lvo-appr-n').textContent = O.approvals.length;
  const C = Object.values(O.cases); $('#lvo-cases').innerHTML = C.length ? C.slice(-6).reverse().map(x => li(x.what, (x.id ? '#' + x.id + ' ' : '') + x.title, x.by)).join('') : '<div class="empty">No case opened or moved in this run.</div>';
  $('#lvo-cases-n').textContent = C.length;
  const R = Object.values(O.records); $('#lvo-rec').innerHTML = R.length ? R.slice(-6).reverse().map(x => li('saved', x.title, x.by)).join('') : '<div class="empty">No record written yet.</div>';
  $('#lvo-rec-n').textContent = R.length;
  $('#lvo-pol').innerHTML = O.policy.length ? O.policy.slice(-4).reverse().map(x => li('blocked', x.v.join(' · '), x.by)).join('') : '<div class="empty">No draft blocked by policy.</div>';
  $('#lvo-pol-n').textContent = O.policy.length;
  $('#lvo-deliv').innerHTML = O.deliverables.length ? O.deliverables.map(f => `<li><span class="tg">file</span><span class="tx">${esc(f)}</span></li>`).join('') : '<div class="empty">Files appear when the lead saves them.</div>';
  $('#lvo-deliv-n').textContent = O.deliverables.length;
}

/* ---------------- header + timeline */
const lvDur = s => s < 60 ? s.toFixed(0) + 's' : Math.floor(s / 60) + 'm ' + String(Math.floor(s % 60)).padStart(2, '0') + 's';
function lvRenderHead(){
  const S = liveState; const n = Object.values(S.agents).filter(a => !a.done && a.status !== 'idle').length;
  $('#lv-status').textContent = S.active ? 'running' : (S.status === 'done' ? 'finished' : S.status || 'finished'); $('#lv-dot').classList.toggle('live', S.active);
  $('#lv-stat-run').classList.toggle('run', S.active);
  $('#lv-in').textContent = (S.tin || 0).toLocaleString(); $('#lv-out').textContent = (S.tout || 0).toLocaleString(); $('#lv-n').textContent = S.n;
  $('#lv-active').textContent = n; $('#lv-stop').style.display = S.active ? '' : 'none';
}
function lvTimeline(){
  const S = liveState, tr = $('#lv-track'); if (!tr) return;
  const t0 = S.t0, t1 = S.active ? Date.now() / 1000 : (S.tEnd || (S.ticks.length ? S.ticks[S.ticks.length - 1].ts : t0 + 1));
  const span = Math.max(5, t1 - t0);
  const lanes = ['atlas', ...CFG.agents.filter(a => a.id !== 'atlas').map(a => a.id)];
  const laneY = k => { const i = Math.max(0, lanes.indexOf(String(k).split('#')[0])); return 6 + i * (42 / Math.max(1, lanes.length - 1 || 1)); };
  let html = lanes.map(k => `<div class="lane" style="top:${laneY(k) + 4}px"></div>`).join('');
  html += S.ticks.map(t => `<div class="tick ${t.cls}" style="left:${((t.ts - t0) / span * 100).toFixed(2)}%;top:${laneY(t.key)}px;height:9px;--c:${t.color}" title="${esc(hms(t.ts))} ${esc(String(t.key))}" onclick="lvGoto(${t.i})"></div>`).join('');
  if (S.active) html += `<div class="cur" style="left:${((t1 - t0) / span * 100).toFixed(2)}%"></div>`;
  tr.innerHTML = html;
  $('#lv-ax0').textContent = hms(t0); $('#lv-ax1').textContent = S.active ? 'now · ' + lvDur(span) : lvDur(span);
}
function lvGoto(i){ const it = liveState.feed[i]; if (!it || !it.el) return; LV.stick = false; it.el.style.display = ''; it.el.scrollIntoView({ behavior: 'smooth', block: 'center' }); it.el.animate([{ boxShadow: '0 0 0 3px rgba(47,111,228,.35)' }, { boxShadow: 'none' }], { duration: 1400 }); }
async function lvStop(){ if (!liveState) return; await api('/runs/' + liveState.id + '/cancel', { method: 'POST', body: {} }); toast('Stopping the run'); }

let lvQueued = false;
function lvSchedule(){ if (lvQueued) return; lvQueued = true; requestAnimationFrame(() => { lvQueued = false; lvRenderAll(); }); }
function lvRenderAll(){ if (!liveState || page !== 'live') return; lvRenderHead(); lvRenderTeam(); lvRenderOut(); lvTimeline(); }

/* ---------------- graph glue */
function lvSyncGraphCounts(){ const O = liveState.out; LVG.setCounts(O.approvals.length, Object.keys(O.cases).length + Object.keys(O.records).length); }
function lvFocusAgent(id){ const S = liveState; if (!S) return; const it = [...S.feed].reverse().find(x => x.a && (x.a.id === id || x.a.base === id)); if (it) lvGoto(it.i); }
function lvReplay(){
  const S = liveState; if (!S || S.active || !S.raw || !S.raw.length || LV.replaying) return;
  LV.replaying = true; const raw = S.raw, id = S.id;
  // rebuild from zero, then play the events back with time compressed so the whole run takes about 40 s
  liveState = { ...S, active: true, agents: {}, n: 0, feed: [], ticks: [], out: { approvals: [], cases: {}, records: {}, deliverables: [], policy: [], sent: 0, tools: 0, handoffs: 0 } };
  lvBuildTeam(); LVG.build(CFG.agents); $('#lv-feed').innerHTML = ''; LV.stick = true;
  const t0 = raw[0].ts, span = Math.max(1, raw[raw.length - 1].ts - t0), scale = Math.min(1, 40 / span);
  let i = 0, start = performance.now(), gap = 0;
  $('#lv-replay').textContent = '■ Stop replay';
  const tickFn = () => {
    if (!LV.replaying || liveState.id !== id) return;
    const now = (performance.now() - start) / 1000;
    while (i < raw.length && ((raw[i].ts - t0) * scale + gap) <= now) { lvApply(raw[i], true); gap += 0.12; i++; }
    lvSchedule();
    if (i < raw.length) LV.rt = setTimeout(tickFn, 60); else { LV.replaying = false; liveState.active = false; $('#lv-replay').textContent = '\u25B6 Replay'; lvSchedule(); }
  };
  liveState.t0 = Date.now() / 1000; tickFn();
}
function lvReplayToggle(){ if (LV.replaying) { LV.replaying = false; clearTimeout(LV.rt); $('#lv-replay').textContent = '\u25B6 Replay'; pickLive(liveState.id); } else lvReplay(); }
