/* Atlas Admin (usage spec §7.2): the operator's view of every account. Reads GET /api/admin/accounts (on load, on
   Refresh / R, every 60 s while visible); a limit change is PUT /api/admin/accounts/<id>/limits. All text from the
   server reaches the page through textContent; no inline handlers, no parsed HTML. */
'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const S = {data: null, open: new Set(), filter: 'real', q: ''};

  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = String(v);
      else e.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of kids.flat()) if (c != null && c !== false && c !== '') e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return e;
  }
  const icon = id => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('class', 'i');
    const u = document.createElementNS('http://www.w3.org/2000/svg', 'use'); u.setAttribute('href', '#' + id); s.append(u); return s; };

  const fmtN = n => Number(n || 0).toLocaleString('en-GB');
  const fmtUsd = v => '$' + Number(v || 0).toFixed(2);
  function amount(lim, v) {
    if (v == null) return 'no limit';
    if (lim.unit === 'usd') return fmtUsd(v);
    if (lim.unit === 'MB') return fmtN(v) + ' MB';
    return fmtN(v);
  }
  function ago(ts) {
    if (!ts) return 'never';
    const d = Date.now() / 1000 - ts;
    if (d < 90) return 'just now';
    if (d < 3600) return Math.round(d / 60) + ' min ago';
    if (d < 86400 * 2) return Math.round(d / 3600) + ' h ago';
    if (d < 86400 * 30) return Math.round(d / 86400) + ' d ago';
    return new Date(ts * 1000).toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric'});
  }
  const WINDOW = {D: 'per UTC day', M: 'per UTC month', T: 'running total', run: 'per run', now: 'at once'};

  async function api(path, opt = {}) {
    const r = await fetch('/api' + path, {method: opt.method || 'GET', headers: {'Content-Type': 'application/json'},
                                          credentials: 'same-origin', body: opt.body ? JSON.stringify(opt.body) : undefined});
    let j = null; try { j = await r.json(); } catch (e) { j = null; }
    if (!r.ok) { const err = new Error((j && (j.message || j.error)) || ('HTTP ' + r.status)); err.status = r.status; err.body = j; throw err; }
    return j;
  }

  // ---------------------------------------------------------------- render
  function frac(used, limit, unit) {
    const el = h('span', {class: 'frac'});
    el.append(unit === 'usd' ? fmtUsd(used) : fmtN(used));
    if (limit != null) {
      el.append(h('span', {class: 'of', text: ' / ' + (unit === 'usd' ? fmtUsd(limit) : fmtN(limit))}));
      const r = limit > 0 ? used / limit : (used > 0 ? 1 : 0);
      if (r >= 1) el.classList.add('bad'); else if (r >= 0.8) el.classList.add('warn');
    } else el.append(h('span', {class: 'of', text: ' / ∞'}));
    return el;
  }
  function limitOf(a, id) { return a.limits.find(l => l.id === id) || {value: null, unit: ''}; }

  function visible(a) {
    if (S.filter === 'real' && a.guest) return false;
    if (S.filter === 'over' && !a.limits.some(l => l.override)) return false;
    if (S.q) { const q = S.q.toLowerCase(); if (!(a.email + ' ' + a.name + ' ' + a.company).toLowerCase().includes(q)) return false; }
    return true;
  }

  function rowFor(a) {
    const open = S.open.has(a.id);
    const tr = h('tr', {'data-id': a.id, class: open ? 'open' : ''});
    const tog = h('button', {class: 'tog', type: 'button', 'aria-label': 'Limits for ' + a.email, 'aria-expanded': open ? 'true' : 'false'}, icon('i-chev'));
    tog.addEventListener('click', () => { if (S.open.has(a.id)) S.open.delete(a.id); else S.open.add(a.id); render(); });
    tr.append(h('td', {}, tog));
    const nOv = a.limits.filter(l => l.override).length;
    const name = h('b', {text: a.name || '—'});
    if (a.guest) name.append(h('i', {class: 'tag guest', text: 'guest'}));
    if (a.unlimited) name.append(h('i', {class: 'tag unl', text: 'unlimited'}));
    if (nOv) name.append(h('i', {class: 'tag ov', text: nOv + (nOv === 1 ? ' override' : ' overrides')}));
    tr.append(h('td', {}, h('div', {class: 'who'}, name, h('span', {text: a.email}))));
    tr.append(h('td', {class: 'co', title: (a.company || '') + (a.role ? ' · ' + a.role : '')}, a.company || '—', a.role ? h('small', {text: a.role}) : ''));
    tr.append(h('td', {class: 'n', text: fmtN(a.desks)}));
    tr.append(h('td', {class: 'n'}, frac(a.today.ai_requests_day, limitOf(a, 'ai_requests_day').value, 'requests')));
    tr.append(h('td', {class: 'n'}, frac(a.today.ai_requests_auto_day, limitOf(a, 'ai_requests_auto_day').value, 'requests')));
    tr.append(h('td', {class: 'n'}, frac(a.today.paid_usd_month, limitOf(a, 'paid_usd_month').value, 'usd')));
    tr.append(h('td', {class: 'n', text: fmtN(a.today.refused)}));
    tr.append(h('td', {text: ago(a.last_login), title: 'created ' + ago(a.created)}));
    const lb = h('button', {class: 'btn sm', type: 'button', text: open ? 'Close' : 'Set limits'});
    lb.addEventListener('click', () => tog.click());
    tr.append(h('td', {}, lb));
    return tr;
  }

  function limitCard(a, l) {
    const card = h('div', {class: 'lim', 'data-lim': l.id});
    card.append(h('div', {class: 'lab'}, l.label, h('small', {text: l.id + ' · ' + (WINDOW[l.window] || l.window)})));
    card.append(h('div', {class: 'cur'}, amount(l, l.value), h('small', {text: l.override ? 'override' : (a.unlimited && l.value == null ? 'unlimited account' : 'default')})));
    if (l.override) card.append(h('div', {class: 'ovn', text: 'set by ' + (l.override.set_by || '?') + ' ' + ago(l.override.updated) + (l.override.note ? ' — ' + l.override.note : '')}));
    const f = h('form', {});
    const inp = h('input', {type: 'text', placeholder: l.unit === 'usd' ? 'e.g. 20 or none' : 'e.g. 500 or none', 'aria-label': 'New ' + l.label, autocomplete: 'off'});
    const note = h('input', {type: 'text', class: 'note', placeholder: 'note (optional)', maxlength: '200', 'aria-label': 'Note'});
    const set = h('button', {class: 'btn sm', type: 'submit', text: 'Set'});
    const def = h('button', {class: 'btn sm', type: 'button', text: 'Default', disabled: !l.override});
    const msg = h('div', {class: 'msg'});
    f.append(inp, note, set, def); card.append(f, msg);
    async function send(value) {
      set.disabled = def.disabled = true; msg.className = 'msg'; msg.textContent = 'Saving…';
      try {
        const r = await api(`/admin/accounts/${a.id}/limits`, {method: 'PUT', body: {limit_id: l.id, value, note: note.value}});
        const i = S.data.accounts.findIndex(x => x.id === a.id); if (i >= 0) S.data.accounts[i] = r.account;
        render();
        const nl = r.account.limits.find(x => x.id === l.id);
        const c = document.querySelector(`tr.lim[data-id="${a.id}"] .lim[data-lim="${l.id}"] .msg`);
        if (c) { c.className = 'msg ok'; c.textContent = `Now ${amount(nl, nl.value)}. Applies within ${r.applies_s} s.`; }
      } catch (e) { msg.className = 'msg bad'; msg.textContent = e.message || 'Could not save.'; set.disabled = false; def.disabled = !l.override; }
    }
    f.addEventListener('submit', ev => { ev.preventDefault(); const v = inp.value.trim(); if (!v) { msg.className = 'msg bad'; msg.textContent = 'Give a number, or none.'; return; } send(v); });
    def.addEventListener('click', () => send('default'));
    return card;
  }

  function limitsRow(a) {
    const tr = h('tr', {class: 'lim', 'data-id': a.id});
    const td = h('td', {colspan: '10'});
    const grid = h('div', {class: 'lims'});
    for (const l of a.limits) grid.append(limitCard(a, l));
    td.append(grid); tr.append(td);
    return tr;
  }

  function render() {
    const d = S.data; if (!d) return;
    const all = d.accounts, real = all.filter(a => !a.guest);
    const shown = all.filter(visible);
    const tb = $('#a-rows'); tb.replaceChildren();
    if (!shown.length) tb.append(h('tr', {}, h('td', {colspan: '10', class: 'none', text: all.length ? 'No account matches.' : 'No accounts yet.'})));
    for (const a of shown) { tb.append(rowFor(a)); if (S.open.has(a.id)) tb.append(limitsRow(a)); }
    const active = real.filter(a => a.today.ai_requests_day > 0 || a.today.ai_requests_auto_day > 0).length;
    const ai = real.reduce((s, a) => s + a.today.ai_requests_day, 0), paid = real.reduce((s, a) => s + a.today.paid_usd_month, 0);
    const refused = all.reduce((s, a) => s + a.today.refused, 0);
    const ov = all.reduce((s, a) => s + a.limits.filter(l => l.override).length, 0);
    const guests = all.length - real.length;
    const kp = [[fmtN(real.length), guests ? `+ ${guests} guest desk${guests === 1 ? '' : 's'}` : 'no guest desks'],
                [fmtN(active), 'used the AI today'], [fmtN(ai), 'across all accounts, today'], [fmtUsd(paid), 'paid models, this month'],
                [fmtN(refused), 'requests a limit stopped'], [fmtN(ov), 'per-account overrides']];
    [...$('#a-stats').children].forEach((el, i) => { el.classList.remove('sk'); el.children[1].textContent = kp[i][0]; el.children[2].textContent = kp[i][1]; });
    $('#a-sub').textContent = d.enforced ? 'Limits are enforced. An override applies within ' + d.applies_s + ' s.' : 'Limits are NOT enforced on this server (USAGE_ENFORCE off): numbers are informational.';
    $('#a-meta').textContent = `${shown.length} of ${all.length}`;
    $('#a-foot').textContent = 'Per-account limits: ' + d.limits.map(l => l.id).join(', ') + '. Server-wide limits (the shared pool, the house paid cap) live in the env file. CLI: python -m atlas usage set --email <email> --limit <id> --value <n|none>.';
    $('#a-upd').textContent = 'updated ' + new Date(d.as_of * 1000).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    $('#up').setAttribute('aria-busy', 'false');
  }

  async function load() {
    try {
      S.data = await api('/admin/accounts');
      $('#a-err').classList.add('hide');
      render();
    } catch (e) {
      $('#a-err').classList.remove('hide');
      $('#a-err-t').textContent = e.status === 403 ? 'This page is for the operator (USAGE_OPERATOR_EMAILS).' : (e.message || 'Could not load accounts.');
      $('#up').setAttribute('aria-busy', 'false');
    }
  }

  $('#a-refresh').addEventListener('click', load);
  $('#a-retry').addEventListener('click', load);
  $('#a-filter').addEventListener('change', e => { S.filter = e.target.value; render(); });
  $('#a-q').addEventListener('input', e => { S.q = e.target.value.trim(); render(); });
  $('#a-theme').addEventListener('click', () => { const r = document.documentElement; r.dataset.theme = r.dataset.theme === 'dark' ? 'light' : 'dark'; try { localStorage.setItem('atlas-theme', r.dataset.theme); } catch (e) {} });
  document.addEventListener('keydown', e => { if (e.key === 'r' && !e.metaKey && !e.ctrlKey && !/input|select|textarea/i.test(e.target.tagName)) load(); });
  setInterval(() => { if (!document.hidden) load(); }, 60000);
  load();
})();
