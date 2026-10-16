/* Live desk inspector: click an agent on the board, configure it in place (name, colour, role, goal, rules, tools,
   who it reports to), see what it did in this run, save to the desk (PATCH /api/desks/<id> {agents}). */
const LVI = (() => {
  const SPECIALIST_OK = new Set(['crm_lookup', 'crm_update', 'queue_action', 'camera_look', 'camera_events', 'camera_ask', 'remember', 'recall', 'browse', 'http_request', 'calendar_free_slots', 'calendar_book', 'generate_media', 'video_describe', 'log_search', 'enrich', 'correlate', 'record_find', 'record_get', 'record_save', 'case_open', 'case_update', 'case_list']);
  const COLORS = ['#7c3aed', '#1f9d63', '#db2777', '#ea580c', '#b45309', '#0891b2', '#2563eb', '#dc2626'];
  const TOOL_GROUPS = [
    ['Records & cases', ['record_find', 'record_get', 'record_save', 'case_open', 'case_update', 'case_list', 'crm_lookup', 'crm_update']],
    ['Outbound (always held for you)', ['queue_action', 'calendar_free_slots', 'calendar_book']],
    ['Cameras', ['camera_look', 'camera_events', 'camera_ask', 'video_describe']],
    ['Research & files', ['read_file', 'list_files', 'web_fetch', 'browse', 'http_request', 'run_python', 'save_deliverable', 'generate_media']],
    ['Memory', ['remember', 'recall']],
    ['Security', ['log_search', 'enrich', 'correlate']],
  ];
  const I = { open: null, draft: null, dirty: false };
  const el = () => document.getElementById('lv-insp');
  const esc_ = s => (typeof esc === 'function' ? esc(s) : String(s));
  const ini = n => String(n || '?').split(/[\s&]+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase();
  const toolInfo = id => (CFG.tools || []).find(t => t.id === id) || { id, description: '' };

  function open(id){
    const a = id ? CFG.agents.find(x => x.id === id) : null;
    I.open = id || '__new__';
    I.draft = a ? JSON.parse(JSON.stringify(a)) : { id: '', name: '', role: '', goal: '', instructions: [''], tools: ['record_find', 'record_get', 'crm_lookup'], color: COLORS[(CFG.agents.length - 1) % COLORS.length], reports_to: 'atlas', enabled: true, system_prompt: '', engine: 'atlas', model: '' };
    I.draft.instructions = (I.draft.instructions && I.draft.instructions.length ? I.draft.instructions : ['']).slice();
    I.dirty = !a; render(); el().classList.add('on'); el().querySelector('input[data-f="name"]').focus();
  }
  function close(){ I.open = null; I.draft = null; I.dirty = false; el().classList.remove('on'); }
  function set(f, v){ I.draft[f] = v; I.dirty = true; renderHead(); }
  function rule(i, v){ I.draft.instructions[i] = v; I.dirty = true; }
  function ruleAdd(){ if (I.draft.instructions.length >= 6) return; I.draft.instructions.push(''); I.dirty = true; render(); setTimeout(() => { const rs = el().querySelectorAll('.lvi-rule input'); rs[rs.length - 1].focus(); }, 0); }
  function ruleDel(i){ I.draft.instructions.splice(i, 1); if (!I.draft.instructions.length) I.draft.instructions.push(''); I.dirty = true; render(); }
  function tool(id, on){ const t = new Set(I.draft.tools || []); on ? t.add(id) : t.delete(id); I.draft.tools = [...t]; I.dirty = true; renderHead(); const b = el().querySelector(`.lvi-tool[data-t="${id}"]`); if (b) b.classList.toggle('on', on); }
  function color(c){ I.draft.color = c; I.dirty = true; render(); }

  function stats(){ const S = typeof liveState !== 'undefined' && liveState; if (!S || !I.draft.id) return null; const a = S.agents[I.draft.id]; if (!a) return null;
    return { turns: a.turns, tools: a.tools, tokens: a.tokens || 0, appr: S.out.approvals.filter(x => x.by === a.name).length, status: a.status, now: a.now, done: a.done }; }

  function renderHead(){ const d = I.draft, h = el().querySelector('.lvi-head'); if (!h) return; h.style.setProperty('--c', d.color);
    h.querySelector('.lvi-av').textContent = ini(d.name || 'New agent'); h.querySelector('.lvi-save').disabled = !I.dirty; h.querySelector('.lvi-tcount').textContent = (d.tools || []).length + ' tools'; }
  function render(){
    const d = I.draft, isLead = d.id === 'atlas', st = stats(), x = el();
    const others = CFG.agents.filter(a => a.id !== d.id);
    const allowed = id => isLead || !toolInfo(id).orchestrator_only || SPECIALIST_OK.has(id);
    x.innerHTML = `
      <div class="lvi-head" style="--c:${d.color}">
        <div class="lvi-av">${ini(d.name || 'New agent')}</div>
        <div class="lvi-title">
          <input data-f="name" value="${esc_(d.name)}" placeholder="${isLead ? 'Atlas' : 'A clear job title: Repairs Desk, Arrears Controller…'}" oninput="LVI.set('name', this.value)" ${isLead ? '' : ''}>
          <input data-f="role" value="${esc_(d.role || '')}" placeholder="One line: what this person is for" oninput="LVI.set('role', this.value)">
        </div>
        <button class="lvi-x" onclick="LVI.close()" title="Close">×</button>
        <div class="lvi-sub"><span class="lvi-tcount">${(d.tools || []).length} tools</span><span>· ${esc_(d.engine === 'host' ? 'hosted session' : (d.model || 'desk default'))}</span>${isLead ? '<span>· leads the team</span>' : ''}<span class="sp"></span><button class="lvi-save" onclick="LVI.save()" ${I.dirty ? '' : 'disabled'}>Save</button></div>
      </div>
      ${st ? `<div class="lvi-stats"><div><b>${st.turns}</b><span>turns</span></div><div><b>${st.tools}</b><span>tool calls</span></div><div><b>${st.tokens ? st.tokens.toLocaleString() : '–'}</b><span>tokens</span></div><div><b>${st.appr}</b><span>held for you</span></div><div class="st ${st.done ? 'done' : ''}"><b>${esc_(st.status)}</b><span>${esc_(st.now || (st.done ? 'finished' : 'this run'))}</span></div></div>` : `<div class="lvi-stats none">${d.id ? 'Not used in this run yet.' : 'New agent: name it, give it one goal and a few rules, pick its tools.'}</div>`}
      <div class="lvi-body">
        <label>Colour</label>
        <div class="lvi-colors">${COLORS.map(c => `<button style="--c:${c}" class="${c === d.color ? 'on' : ''}" onclick="LVI.color('${c}')"></button>`).join('')}</div>
        ${isLead ? '' : `<label>Goal <small>one line, what done looks like</small></label>
        <textarea rows="2" oninput="LVI.set('goal', this.value)" placeholder="e.g. Every repair has a contractor request, a cost estimate and a tenant update drafted for approval">${esc_(d.goal || '')}</textarea>
        <label>Rules <small>≤ 3 is best</small></label>
        <div class="lvi-rules">${d.instructions.map((r, i) => `<div class="lvi-rule"><span>${i + 1}</span><input value="${esc_(r)}" placeholder="Never state a repair date before the contractor confirms" oninput="LVI.rule(${i}, this.value)"><button onclick="LVI.ruleDel(${i})" title="Remove">×</button></div>`).join('')}
          ${d.instructions.length < 6 ? '<button class="lvi-add" onclick="LVI.ruleAdd()">+ rule</button>' : ''}</div>
        <label>Reports to</label>
        <select onchange="LVI.set('reports_to', this.value)">${[{ id: 'atlas', name: 'Atlas (lead)' }, ...others.filter(a => a.id !== 'atlas')].map(a => `<option value="${a.id}" ${(d.reports_to || 'atlas') === a.id ? 'selected' : ''}>${esc_(a.name)}</option>`).join('')}</select>`}
        <label>Tools <small>⏸ = always waits for your approval</small></label>
        ${TOOL_GROUPS.map(([g, ids]) => { const vis = ids.filter(id => toolInfo(id) && (CFG.tools || []).some(t => t.id === id)); if (!vis.length) return ''; return `<div class="lvi-group"><div class="lvi-gname">${g}</div><div class="lvi-tools">${vis.map(id => { const on = (d.tools || []).includes(id), ok = allowed(id); return `<button class="lvi-tool ${on ? 'on' : ''} ${ok ? '' : 'na'}" data-t="${id}" title="${esc_(toolInfo(id).description || '')}${ok ? '' : ' (Atlas only)'}" ${ok ? `onclick="LVI.tool('${id}', !this.classList.contains('on'))"` : 'disabled'}>${['queue_action', 'calendar_book', 'browse', 'http_request'].includes(id) ? '⏸ ' : ''}${id}</button>`; }).join('')}</div></div>`; }).join('')}
        ${isLead ? `<label>Brief <small>Atlas's standing instructions for this desk</small></label><textarea rows="6" oninput="LVI.set('system_prompt', this.value)">${esc_(d.system_prompt || '')}</textarea>` : `<label>Extra brief <small>optional, plain words</small></label><textarea rows="3" oninput="LVI.set('system_prompt', this.value)" placeholder="Anything else this agent must know">${esc_(d.system_prompt || '')}</textarea>`}
      </div>
      <div class="lvi-foot">
        ${d.id && !isLead ? `<button class="lvi-danger" onclick="LVI.remove()">Remove agent</button>` : ''}
        ${d.id ? `<button onclick="lvFocusAgent('${d.id}')">Find in feed</button>` : ''}
        <span class="sp"></span>
        <button onclick="LVI.close()">Close</button>
        <button class="p" onclick="LVI.save()">${d.id ? 'Save' : 'Add to team'}</button>
      </div>`;
  }

  function payload(){
    const slug = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24);
    const used = new Set(CFG.agents.map(a => a.id)); const d = I.draft;
    const clean = a => ({ id: a.id, name: (a.name || a.id).trim(), role: (a.role || '').trim(), system_prompt: a.system_prompt || '', tools: a.tools || [], enabled: a.enabled !== false, color: a.color, pos: a.pos || undefined, reports_to: a.reports_to || 'atlas', goal: a.goal || '', instructions: (a.instructions || []).filter(r => r && r.trim()) });
    let roster = CFG.agents.map(a => a.id === d.id ? clean(d) : clean(a));
    if (!d.id) { let id = slug(d.name) || 'agent'; let n = 0; while (used.has(id) || ['atlas', 'system', 'owner'].includes(id)) id = id.slice(0, 20) + '_' + (++n); roster.push({ ...clean(d), id }); }
    return roster.map(a => a.id === 'atlas' ? { id: 'atlas', name: a.name || 'Atlas', role: a.role, system_prompt: a.system_prompt, tools: a.tools, enabled: true, color: a.color, pos: a.pos } : a);
  }
  async function save(){
    const d = I.draft; if (!d.name || !d.name.trim()) return toast('Give the agent a name');
    if (d.id !== 'atlas' && !(d.instructions || []).some(r => r && r.trim())) return toast('Give it at least one rule');
    const did = (CFG.desk || {}).id; if (!did) return toast('No desk selected');
    const r = await api(`/desks/${did}`, { method: 'PATCH', body: { agents: payload() } });
    if (!r || r.error) return toast((r && r.error) || 'could not save');
    toast(d.id ? 'Saved. Applies to the next job.' : 'Added to the team. Applies to the next job.');
    I.dirty = false; await loadCfg(); if (typeof lvAfterTeamChange === 'function') lvAfterTeamChange(); close();
  }
  async function remove(){
    const d = I.draft; if (!d.id || d.id === 'atlas') return;
    const x = el().querySelector('.lvi-danger'); if (x && !x.dataset.sure) { x.dataset.sure = '1'; x.textContent = 'Really remove ' + d.name + '?'; setTimeout(() => { if (x.isConnected) { delete x.dataset.sure; x.textContent = 'Remove agent'; } }, 3500); return; }
    const did = (CFG.desk || {}).id; const roster = payload().filter(a => a.id !== d.id);
    const r = await api(`/desks/${did}`, { method: 'PATCH', body: { agents: roster } });
    if (!r || r.error) return toast((r && r.error) || 'could not remove');
    toast(d.name + ' removed'); await loadCfg(); if (typeof lvAfterTeamChange === 'function') lvAfterTeamChange(); close();
  }
  return { open, close, set, rule, ruleAdd, ruleDel, tool, color, save, remove, state: I };
})();
