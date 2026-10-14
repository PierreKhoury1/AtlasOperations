/* Atlas app frame: the same navigation, search (Ctrl K) and Ask Atlas panel on every page of the desk.
   Each page keeps its own content; this adds the frame around it. Pages opt in with shell.css + this script.
   A page can register window.shellPage = {ask(mode, text), commands(), replaces: [...]} before this script boots:
   the rail's Ask Atlas then opens that page's own Atlas, and its commands lead the Ctrl K palette. */

/* Usage limits, one formatter for every page that loads this script (usage spec §4.8): numbers first, then the reset
   as "00:00 UTC (in 6 h 27 min)" with the viewer's local time in the tooltip, then a link to the Usage page. A 429
   usage_limit, a 409 outside_window (a channel's 24-hour reply window) and a 429 rate_limited body all read the same.
   .text(body) for toasts and plain lines, .html(body) for a line with the link, .link(body) for the reset and the link
   alone, .map(body) for an api() helper: the formatted text in `error`, `limited: true`, the original code in `code`. */
(() => {
  if (window.atlasLimit) return;
  const KINDS = ['usage_limit', 'outside_window', 'rate_limited'];
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const pad = n => String(n).padStart(2, '0');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  // a body as the server sent it, or as an api() helper mapped it (the formatted text in `error`, the code in `code`)
  const kindOf = b => !b || typeof b !== 'object' ? '' : KINDS.includes(b.error) ? b.error : b.limited && KINDS.includes(b.code) ? b.code : '';
  const is = b => !!kindOf(b);
  function dur(s) {
    s = Math.max(0, Math.ceil(s));
    if (s < 60) return s + ' s';
    const m = Math.floor(s / 60); if (m < 60) return m + ' min';
    const h = Math.floor(m / 60), r = m % 60; if (h < 48) return r ? h + ' h ' + r + ' min' : h + ' h';
    const d = Math.floor(h / 24), rh = h % 24; return rh ? d + ' d ' + rh + ' h' : d + ' d';
  }
  function reset(b) {                                    // when it works again, from the body's own times
    const now = Date.now() / 1000;
    if (kindOf(b) === 'rate_limited') return b.retry_after_s ? {lead: 'Try again', in: dur(b.retry_after_s), soon: true} : null;
    if (b.limit_id === 'pool_free_min' && b.retry_at) return {lead: 'Try again', in: dur(b.retry_at - now), soon: true};
    const ts = b.resets_at; if (!ts) return null;        // the per-run cap, files, a reply window: nothing resets by itself
    const d = new Date(ts * 1000), far = ts - now > 86400;
    let local = '';
    try { local = d.toLocaleString([], {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) + ' your time'; } catch (e) {}
    return {lead: 'Resets', utc: pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ' UTC' + (far ? ' on ' + d.getUTCDate() + ' ' + MON[d.getUTCMonth()] : ''),
            in: dur(ts - now), local};
  }
  // the server's message without its own reset sentence: that one was true when it was written, this one is now
  const strip = m => String(m || '').replace(/\s*Resets \d\d:\d\d UTC[^.]*\./, '').replace(/\s*Try again in [^.]*\./, '').trim();
  const msg = b => strip(b.message) || b.label || 'A usage limit stopped this.';
  function text(b) {
    if (!is(b)) return String((b && (b.message || b.error)) || '');
    const r = reset(b);
    return msg(b) + (r ? (r.soon ? ' ' + r.lead + ' in ' + r.in + '.' : ' ' + r.lead + ' ' + r.utc + ' (in ' + r.in + ').') : '');
  }
  function link(b) {
    const r = is(b) ? reset(b) : null, out = [];
    if (r) out.push(r.soon ? '<span class="al-reset">' + esc(r.lead) + ' in ' + esc(r.in) + '</span>'
                           : '<span class="al-reset" title="' + esc(r.local) + '">' + esc(r.lead) + ' ' + esc(r.utc) + ' (in ' + esc(r.in) + ')</span>');
    if (kindOf(b) !== 'rate_limited') out.push('<a class="al-link" href="/desk/usage">See usage</a>');
    return out.join(' · ');
  }
  const html = b => '<span class="al-msg">' + esc(is(b) ? msg(b) : text(b)) + '</span> ' + link(b);
  const map = b => is(b) && !b.limited ? {...b, code: b.error, error: text(b), limited: true} : b;
  window.atlasLimit = {is, text, html, link, map};
})();

(() => {
  if (window.__shell || new URLSearchParams(location.search).has('embed')) return;
  window.__shell = true;
  const IC = {
    home: '<path d="M3 11 12 3l9 8"/><path d="M5 9.5V21h14V9.5"/>', inbox: '<path d="M3 13h5l1.5 3h5L16 13h5"/><path d="M5 5h14l2 8v6H3v-6z"/>',
    check: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m8 12 3 3 5-6"/>', pulse: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    leads: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.5 3-6 6.5-6s6.5 2.5 6.5 6"/><path d="M17 8h5M19.5 5.5v5"/>',
    db: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    camera: '<path d="M3 8h4l2-3h6l2 3h4v11H3z"/><circle cx="12" cy="13" r="3.5"/>', grid: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
    film: '<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M3 20h18M7 20v-4M12 20v-4M17 20v-4"/>', shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    team: '<circle cx="12" cy="6" r="3"/><circle cx="6" cy="17" r="3"/><circle cx="18" cy="17" r="3"/><path d="M10.5 8.5 7.5 14M13.5 8.5l3 5.5M9 17h6"/>',
    spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', list: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>',
    link: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
    doc: '<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6"/><path d="M9 13h7M9 17h7"/>', chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>', chat: '<path d="M4 5h16v11H9l-5 4z"/>', chev: '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
    side: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>', plus: '<path d="M12 5v14M5 12h14"/>', out: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>', tick: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    play: '<path d="M7 4.5v15l12-7.5z"/>', x: '<path d="M6 6l12 12M18 6 6 18"/>', send: '<path d="M4 12 20 4l-6 16-2.5-6.5L4 12Z"/>',
    gauge: '<path d="M4 15a8 8 0 1 1 16 0"/><path d="m12 15 4-5"/>',
  };
  const svg = k => `<svg viewBox="0 0 24 24">${IC[k] || ''}</svg>`;
  const NAV = [
    ['Work', [['home', 'Home', '/desk#dash', 'home'], ['cases', 'Cases', '/desk/ops', 'inbox'], ['inbox', 'Messages', '/desk#inbox', 'chat'], ['studio', 'Ad studio', '/desk#studio', 'spark'], ['approvals', 'Approvals', '/desk#approvals', 'check'],
              ['live', 'Live runs', '/desk#live', 'pulse'], ['leads', 'Leads', '/desk#leads', 'leads'], ['crm', 'Contacts', '/desk#crm', 'db']]],
    ['See', [['cameras', 'Cameras', '/desk#cameras', 'camera'], ['objects', 'Objects', '/desk/objects', 'grid'], ['review', 'Review', '/desk/review', 'film'],
             ['security', 'Security', '/desk/cyber', 'shield']]],
    ['Team', [['team', 'Agents', '/desk#team', 'team'], ['design', 'Atlas workspace', '/desk/workspace', 'spark'], ['automations', 'Automations', '/desk#automations', 'clock'],
              ['runs', 'Run history', '/desk#runs', 'list']]],
    ['Connect', [['integrations', 'Integrations', '/desk#integrations', 'link'], ['audit', 'Audit log', '/desk#audit', 'doc'], ['report', 'Reports', '/desk#report', 'chart'],
                 ['usage', 'Usage', '/desk/usage', 'gauge'], ['setup', 'Desk setup', '/desk#setup', 'gear']]],
  ];
  // an owner with no desk yet: the same frame, with only the way in (Atlas drafts the first desk in the workspace)
  // (design chat counts against the account's allowance before any desk exists, so Usage is here too)
  const NAV_SETUP = [['Start', [['design', 'Atlas workspace', '/desk/workspace', 'spark'], ['home', 'Other ways to start', '/desk', 'grid'],
                                ['usage', 'Usage', '/desk/usage', 'gauge']]]];
  let SETUP = false;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const isIndex = () => location.pathname === '/desk' || location.pathname === '/desk/';
  function current() {
    const p = location.pathname.replace(/\/$/, '');
    if (p === '/desk/ops') return 'cases';
    if (p === '/desk/objects') return 'objects';
    if (p === '/desk/review') return 'review';
    if (p === '/desk/cyber' || p === '/desk/static/cyber.html') return 'security';
    if (p === '/desk/workspace') return 'design';
    if (p === '/desk/usage') return 'usage';
    const h = location.hash.slice(1) || 'dash';
    return h === 'dash' ? 'home' : h;
  }
  // the workspace shows ONE desk (?desk=N): the frame reads that desk too, whatever another tab selected meanwhile
  function pinned() {
    if (current() !== 'design') return 0;
    const v = parseInt(new URLSearchParams(location.search).get('desk') || '', 10);
    return v > 0 ? v : 0;
  }
  async function api(path, opt = {}) {
    const headers = {'Content-Type': 'application/json'}; const pd = pinned(); if (pd) headers['X-Desk'] = String(pd);
    const r = await fetch('/api' + path, {method: opt.method || 'GET', headers, credentials: 'same-origin',
                                           body: opt.body ? JSON.stringify(opt.body) : undefined});
    if (r.status === 401) return null;
    try { return window.atlasLimit.map(await r.json()); } catch (e) { return null; }   // usage_limit / outside_window -> the formatted message
  }
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} return null; };
  const S = {desks: [], cur: null, me: null, stats: null, cams: 0, agents: [], cameras: [], cases: []};
  const page = () => { const p = window.shellPage; return p && typeof p === 'object' ? p : null; };

  // Ask Atlas: a page with its own Atlas conversation (the workspace) takes it; a new owner is sent to the workspace
  function askAtlas(mode, text) {
    const p = page();
    if (p && typeof p.ask === 'function') { try { if (p.ask(mode, text) !== false) return; } catch (e) {} }
    if (SETUP) { location.href = '/desk/workspace'; return; }
    ask.open(mode, text);
  }

  // ---------------------------------------------------------------- rail
  let railWired = false;
  function rail() {
    const r = document.createElement('div'); r.id = 'sh-rail'; r.setAttribute('role', 'navigation');   // not <nav>: the pages style bare nav elements
    const nav = SETUP ? NAV_SETUP : NAV;
    r.innerHTML = `<div class="sh-top"><a class="sh-brand" href="${SETUP ? '/desk/workspace' : '/desk#dash'}" data-go="dash"><i></i><span>Atlas</span></a>
        <button class="sh-iconbtn" id="sh-mini" title="Collapse the menu">${svg('side')}</button></div>
      <button class="sh-desk" id="sh-desk" title="${SETUP ? 'Start a desk' : 'Switch desk'}">${svg('home')}<span class="nm"><b id="sh-dname">${SETUP ? 'No desk yet' : '…'}</b><small id="sh-dsub">${SETUP ? 'Atlas drafts it with you' : 'loading'}</small></span>${svg('chev')}</button>
      <div class="sh-actions"><button class="sh-act ask" id="sh-askbtn" title="${SETUP ? 'Describe the business to Atlas' : 'Give the team a job or ask the cameras'}">${svg('chat')}<span>Ask Atlas</span></button>
        <button class="sh-act" id="sh-search" title="Search and jump anywhere">${svg('search')}<span>Search</span>${location.pathname.startsWith('/desk/ops') ? '' : '<kbd>Ctrl K</kbd>'}</button></div>
      <div class="sh-nav">${nav.map(([g, items]) => `<div class="sh-grp">${g}</div>` + items.map(([id, label, href, ic]) =>
        `<a class="sh-item" data-id="${id}" href="${href}" title="${esc(label)}">${svg(ic)}<span class="lbl">${esc(label)}</span><span class="sh-badge" id="sh-b-${id}" hidden></span></a>`).join('')).join('')}</div>
      <div class="sh-foot"><div class="sh-av" id="sh-av">?</div><div class="sh-who"><b id="sh-me">…</b><small id="sh-co"></small></div>
        <button class="sh-iconbtn" id="sh-acct" title="Account">${svg('gear')}</button></div>`;
    document.body.prepend(r);
    document.body.classList.add('sh-on');
    document.body.classList.toggle('sh-setup', SETUP);
    if (store('sh-mini') === '1') document.body.classList.add('sh-mini');
    r.querySelector('#sh-mini').onclick = () => { const m = document.body.classList.toggle('sh-mini'); store('sh-mini', m ? '1' : '0'); window.dispatchEvent(new Event('resize')); };
    r.querySelector('#sh-askbtn').onclick = () => askAtlas();
    r.querySelector('#sh-search').onclick = () => pal.open();
    r.querySelector('#sh-desk').onclick = e => deskMenu(e.currentTarget);
    r.querySelector('#sh-acct').onclick = e => acctMenu(e.currentTarget);
    r.addEventListener('click', e => {                       // on the main desk page, its own sections switch without a reload
      const a = e.target.closest('a[href^="/desk#"]'); if (!a || !isIndex() || typeof window.go !== 'function') return;
      e.preventDefault(); const sec = a.getAttribute('href').split('#')[1]; window.go(sec); mark();
    });
    mark();
    if (!railWired) { railWired = true; window.addEventListener('hashchange', () => mark()); }
    if (isIndex()) { const og = window.go; if (typeof og === 'function' && !og.__sh) { window.go = function (p) { const r = og.apply(this, arguments); mark(p); return r; }; window.go.__sh = true; } }
  }
  function mark(p) {
    const cur = p ? (p === 'dash' ? 'home' : p) : current();
    document.querySelectorAll('#sh-rail .sh-item').forEach(a => a.classList.toggle('on', a.dataset.id === cur));
  }
  function badge(id, n, cls) {
    const b = document.getElementById('sh-b-' + id); if (!b) return;
    b.hidden = !n; b.textContent = n > 99 ? '99+' : n; b.className = 'sh-badge' + (cls ? ' ' + cls : '');
  }
  // the usage badge (usage spec §4.8): "!" in the warn colour from 80% of any of the account's limits, hot at 100%
  async function usageBadge() {
    const b = await api('/usage/brief'), el = document.getElementById('sh-b-usage'); if (!el || !b || !b.state) return;
    el.hidden = b.state === 'ok'; el.textContent = '!'; el.className = 'sh-badge ' + (b.state === 'full' ? 'hot' : 'warn');
    const w = b.worst; el.title = w ? (w.used == null ? w.label : w.label + ': ' + w.used + ' of ' + w.limit + (w.unit && w.unit !== 'requests' ? ' ' + w.unit : '')) : '';
  }
  async function refreshBadges() {
    if (SETUP) return usageBadge();                        // no desk yet: design chat still uses the account's allowance
    usageBadge();
    const s = await api('/stats'); if (!s || s.needs_desk) return; S.stats = s;
    badge('approvals', s.pending || 0, 'hot');
    badge('cases', (s.cases && s.cases.you) || 0, 'hot');
    badge('live', s.active_runs || 0, 'live');
    badge('leads', s.leads || 0);
  }
  const closeMenus = () => document.querySelectorAll('.sh-menu').forEach(m => m.remove());   // Escape or a click outside
  function menu(anchor, html) {
    closeMenus();
    const m = document.createElement('div'); m.className = 'sh-menu'; m.innerHTML = html;
    const r = anchor.getBoundingClientRect(); m.style.left = Math.min(r.left, innerWidth - 250) + 'px';
    document.body.append(m);
    const top = r.bottom + 6 + m.offsetHeight > innerHeight ? r.top - m.offsetHeight - 6 : r.bottom + 6;
    m.style.top = Math.max(8, top) + 'px';
    setTimeout(() => document.addEventListener('click', function h(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('click', h); } }), 0);
    return m;
  }
  async function switchDesk(id) {
    await api(`/desks/${id}/select`, {method: 'POST', body: {}});
    if (current() === 'design') { location.href = '/desk/workspace?desk=' + id; return; }   // the workspace stays the workspace
    location.href = '/desk#dash'; location.reload();
  }
  function deskMenu(btn) {
    if (SETUP) { menu(btn, `<a href="/desk/workspace?new=1">${svg('spark')}New desk with Atlas</a><a href="/desk">${svg('grid')}Other ways to start</a>`); return; }
    const nm = d => d.business_name || d.name, same = {};                  // two desks with one name: each says which it is
    S.desks.forEach(d => { same[nm(d)] = (same[nm(d)] || 0) + 1; });
    const sub = d => same[nm(d)] > 1 ? `<small>#${Number(d.id)} · ${esc(String(d.template || 'custom').replace(/_/g, ' '))}${d.created ? ' · ' + esc(new Date(d.created * 1000).toLocaleDateString('en-GB', {day: 'numeric', month: 'short'})) : ''}</small>` : '';
    const m = menu(btn, S.desks.map(d => `<button data-d="${d.id}" class="${d.id === S.cur ? 'cur' : ''}">${svg('home')}<span class="t">${esc(nm(d))}${sub(d)}</span>${d.id === S.cur ? `<span class="tk">${svg('tick')}</span>` : ''}</button>`).join('')
      + `<hr><a href="/desk/workspace?new=1">${svg('plus')}New desk with Atlas</a><a href="/desk#setup">${svg('gear')}Desk setup</a>`);
    m.querySelectorAll('[data-d]').forEach(b => b.onclick = async () => {
      if (+b.dataset.d === S.cur) return m.remove();
      await switchDesk(+b.dataset.d);
    });
  }
  function acctMenu(btn) {
    const dark = document.documentElement.dataset.theme !== 'light';
    const m = menu(btn, `<button id="sh-th">${svg('moon')}${dark ? 'Light theme' : 'Dark theme'}</button>${SETUP ? '' : `<a href="/desk#setup">${svg('gear')}Desk setup</a>`}<a href="/">${svg('home')}Website</a><hr><a href="/logout">${svg('out')}Log out</a>`);
    m.querySelector('#sh-th').onclick = () => {
      const t = dark ? 'light' : 'dark'; document.documentElement.dataset.theme = t; store('atlas-theme', t);
      if (typeof window.themeLabel === 'function') window.themeLabel(); m.remove();
    };
  }
  function setMe(me) {
    if (!document.getElementById('sh-me')) return;
    if (me && me.id) {
      document.getElementById('sh-me').textContent = me.name || me.email; document.getElementById('sh-co').textContent = me.email || '';
      document.getElementById('sh-av').textContent = (me.name || me.email || '?').trim()[0].toUpperCase();
    } else { document.getElementById('sh-me').textContent = 'Guest'; document.getElementById('sh-co').textContent = 'open mode'; }
  }
  async function loadMe() { S.me = await api('/me'); setMe(S.me); }
  async function loadMeta() {
    const [d, me, cfg, cams, cases] = await Promise.all([api('/desks'), api('/me'), api('/config'), api('/cameras'), api('/cases?open=1&limit=50')]);
    if (d && d.desks) {
      S.desks = d.desks; S.cur = (pinned() && d.desks.some(x => x.id === pinned())) ? pinned() : d.current;
      const cur = d.desks.find(x => x.id === S.cur) || d.desks[0];
      if (cur) document.getElementById('sh-dname').textContent = cur.business_name || cur.name;
      { const md = document.getElementById('sh-mdesk'); if (md && cur) md.textContent = cur.business_name || cur.name; }
      const ws = document.querySelector('#sh-rail .sh-item[data-id="design"]'); if (ws && cur) ws.href = '/desk/workspace?desk=' + cur.id;   // this desk's agents, cameras and chat
    }
    if (cfg && cfg.agents) S.agents = cfg.agents;
    // green only while a camera is really live (watched AND sending frames: the server's status), never for a switched-on dead feed
    if (cams && cams.cameras) { S.cameras = cams.cameras; badge('cameras', cams.cameras.length, cams.cameras.some(c => c.status && c.status.state === 'live') ? 'live' : ''); }
    if (cases && cases.cases) S.cases = cases.cases;
    const specs = S.agents.filter(a => a.id !== 'atlas' && a.enabled !== false).length;      // the same count every page shows
    const sub = []; if (S.agents.length) sub.push(specs ? `Atlas + ${specs} specialist${specs === 1 ? '' : 's'}` : 'Atlas only'); if (S.cameras.length && !S.agents.length) sub.push(`${S.cameras.length} camera${S.cameras.length === 1 ? '' : 's'}`);   // cameras have their own badge below
    document.getElementById('sh-dsub').textContent = sub.join(' · ') || (cfg && cfg.mode === 'live' ? 'live' : 'desk');
    S.me = me; setMe(me);
  }

  // ---------------------------------------------------------------- palette (Ctrl K)
  const pal = {
    el: null, items: [], sel: 0,
    build() {
      const p = page(); let pageCmds = [];
      if (p && typeof p.commands === 'function') { try { pageCmds = p.commands() || []; } catch (e) { pageCmds = []; } }
      const skip = new Set((p && p.replaces) || []);
      const nav = (SETUP ? NAV_SETUP : NAV).flatMap(([g, items]) => items.map(([id, label, href, ic]) => ({g: 'Go to', label, ic, href, kw: g})));
      if (SETUP) return [...pageCmds, {g: 'Do', label: 'Start a new desk with Atlas', ic: 'spark', href: '/desk/workspace?new=1'},
                         {g: 'Do', label: 'Other ways to start: a ready-made desk, or from scratch', ic: 'grid', href: '/desk'}, ...nav];
      const act = [
        {g: 'Do', label: 'Give the team a job', ic: 'chat', run: () => askAtlas('job')},
        {g: 'Do', label: 'Ask the cameras a question', ic: 'camera', run: () => askAtlas('ask')},
        {g: 'Do', label: 'New case', ic: 'inbox', href: '/desk/ops', after: 'newCase'},
        ...(skip.has('add-camera') ? [] : [{g: 'Do', label: 'Add a camera', ic: 'camera', href: '/desk#cameras'}]),
        {g: 'Do', label: 'Add an agent', ic: 'team', href: '/desk#team', after: 'teamAddReady'},
        {g: 'Do', label: 'Connect an app (email, WhatsApp, calendar…)', ic: 'link', href: '/desk#integrations'},
        {g: 'Do', label: 'Change the team with Atlas', ic: 'spark', run: () => askAtlas('design')},
        {g: 'Do', label: 'Start a new desk with Atlas', ic: 'spark', href: '/desk/workspace?new=1'},
      ];
      const ag = skip.has('agents') ? [] : S.agents.filter(a => a.id !== 'atlas').map(a => ({g: 'Agents', label: a.name, hint: a.role, ic: 'team', href: '/desk#team', after: 'teamOpenId:' + a.id}));
      const cm = skip.has('cameras') ? [] : S.cameras.map(c => ({g: 'Cameras', label: c.name, hint: (c.seen && c.seen.counts) ? Object.entries(c.seen.counts).map(([k, n]) => `${n} ${k}`).join(', ') : c.source_kind, ic: 'camera', href: '/desk#cameras', after: 'openLive:' + c.id}));
      const cs = S.cases.map(c => ({g: 'Open cases', label: c.title || ('Case ' + c.id), hint: c.state || c.type, ic: 'inbox', href: '/desk/ops#case/' + c.id}));
      const dk = S.desks.filter(d => d.id !== S.cur).map(d => ({g: 'Switch desk', label: d.business_name || d.name, ic: 'home', run: () => switchDesk(d.id)}));
      return [...pageCmds, ...act, ...nav, ...ag, ...cm, ...cs, ...dk];
    },
    open() {
      if (this.el) return;
      const el = this.el = document.createElement('div'); el.id = 'sh-pal';
      el.innerHTML = `<div class="box"><input placeholder="${SETUP ? 'Search, or type what you want to do…' : 'Search pages, agents, cameras, cases, or type what you want to do…'}" autocomplete="off"><div class="list"></div>
        <div class="ft"><span>↑↓ choose</span><span>Enter open</span><span>Esc close</span></div></div>`;
      document.body.append(el);
      const inp = el.querySelector('input'); this.all = this.build(); this.render('');
      inp.oninput = () => { this.sel = 0; this.render(inp.value); };
      inp.onkeydown = e => {
        if (e.key === 'ArrowDown') { e.preventDefault(); this.sel = Math.min(this.sel + 1, this.items.length - 1); this.render(inp.value, true); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); this.sel = Math.max(this.sel - 1, 0); this.render(inp.value, true); }
        else if (e.key === 'Enter') { e.preventDefault(); if (this.items[this.sel]) this.go(this.items[this.sel]); else if (inp.value.trim()) { const t = inp.value.trim(); this.close(); askAtlas('job', t); } }
        else if (e.key === 'Escape') this.close();
      };
      el.onclick = e => { if (e.target === el) this.close(); };
      inp.focus();
    },
    render(q, keep) {
      const t = q.trim().toLowerCase();
      const score = it => { const s = (it.label + ' ' + (it.hint || '') + ' ' + it.g + ' ' + (it.kw || '')).toLowerCase(); if (!t) return 1; if (s.includes(t)) return 2 + (it.label.toLowerCase().startsWith(t) ? 1 : 0); return t.split(/\s+/).every(w => s.includes(w)) ? 1 : 0; };
      // a page can rank its own entries (rank +1: its navigation and commands; -1: entries that start work), so a few
      // letters never put a run above where the owner wanted to go
      this.items = this.all.map(it => [score(it), it]).filter(([s]) => s > 0).sort((a, b) => (b[0] + (b[1].rank || 0)) - (a[0] + (a[1].rank || 0))).map(([, it]) => it).slice(0, 40);
      if (!t) this.items = this.all.slice(0, 40);
      const list = this.el.querySelector('.list');
      if (!this.items.length) { list.innerHTML = `<div class="none">${SETUP || page() ? 'Nothing matches. Press Enter to send this to Atlas.' : 'Nothing matches. Press Enter to give the team this as a job.'}</div>`; return; }
      let g = '';
      list.innerHTML = this.items.map((it, i) => { const head = it.g !== g ? `<div class="g">${esc(it.g)}</div>` : ''; g = it.g;
        const hint = String(it.hint || '');                     // the whole hint, ended with an ellipsis by CSS where the row runs out
        return head + `<div class="it ${i === this.sel ? 'sel' : ''}" data-i="${i}"${hint ? ` title="${esc(hint)}"` : ''}>${svg(it.ic)}<span>${esc(it.label)}</span>${hint ? `<small>${esc(hint.slice(0, 240))}</small>` : ''}</div>`; }).join('');
      list.querySelectorAll('.it').forEach(d => { d.onclick = () => this.go(this.items[+d.dataset.i]); d.onmousemove = () => { if (this.sel !== +d.dataset.i) { this.sel = +d.dataset.i; this.render(q, true); } }; });
      if (keep) { const s = list.querySelector('.it.sel'); if (s) s.scrollIntoView({block: 'nearest'}); }
    },
    go(it) {
      this.close();
      if (it.run) return it.run();
      if (it.after) store('sh-after', it.after);
      const [path, hash] = it.href.split('#');
      if (isIndex() && path === '/desk' && typeof window.go === 'function') { window.go(hash || 'dash'); mark(hash); runAfter(); return; }
      location.href = it.href;
      if (path === location.pathname && hash !== undefined) location.reload();
    },
    close() { if (this.el) { this.el.remove(); this.el = null; } },
  };
  // an action that needs the page it lives on: retried until it took effect (the page loads its data after this script)
  const DONE = {openLive: () => document.getElementById('live-img'), teamAddReady: () => document.getElementById('tm-i-name'),
                newCase: () => { const v = document.getElementById('dlg-veil'); return v && !v.classList.contains('hide'); },
                teamDesignFor: () => { const p = document.getElementById('tm-proposal'); return p && !p.classList.contains('hide'); }};
  function runAfter() {
    const a = store('sh-after'); if (!a) return; store('sh-after', '');
    const i = a.indexOf(':'), fn = i < 0 ? a : a.slice(0, i), arg = i < 0 ? '' : decodeURIComponent(a.slice(i + 1));
    let n = 0;
    const t = setInterval(() => {
      n++;
      const f = window[fn];
      if (typeof f === 'function' && n > 3) {
        try { arg ? f(isNaN(+arg) ? arg : +arg) : f(); } catch (e) {}
        if (!DONE[fn] || DONE[fn]()) clearInterval(t);
      }
      if (n > 40) clearInterval(t);
    }, 400);
  }

  // ---------------------------------------------------------------- Ask Atlas panel
  const ask = {
    el: null, mode: 'job',
    build() {
      const el = this.el = document.createElement('aside'); el.id = 'sh-ask';
      el.innerHTML = `<div class="hd"><i class="mark"></i><div><b>Ask Atlas</b><small>your team lead: briefs the agents, checks their work</small></div><button title="Close (Esc)">${svg('x')}</button></div>
        <div class="tabs"><button data-m="job">Give a job</button><button data-m="ask">Ask the cameras</button><button data-m="design">Change the team</button></div>
        <div class="bd" id="sh-askbd"></div>
        <div class="cmp"><textarea id="sh-asktx" rows="2"></textarea><button class="send" id="sh-asksend" title="Send (Enter)">${svg('send')}Send</button></div>
        <div class="foot">Nothing leaves without your approval · <a href="/desk#approvals">Approvals</a> · <a href="/desk#live">Live runs</a></div>`;
      document.body.append(el);
      el.querySelector('.hd button').onclick = () => this.close();
      el.querySelectorAll('.tabs button').forEach(b => b.onclick = () => this.setMode(b.dataset.m));
      const tx = el.querySelector('#sh-asktx');
      tx.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); } if (e.key === 'Escape') this.close(); };
      el.querySelector('#sh-asksend').onclick = () => this.send();
    },
    setMode(m) {
      this.mode = m;
      this.el.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.m === m));
      const bd = this.el.querySelector('#sh-askbd'), tx = this.el.querySelector('#sh-asktx');
      const intro = {
        job: ['Any task: Atlas plans it, briefs the right agents, checks their work. Anything outbound waits for your approval.',
              ['Summarise what the cameras saw today', 'Draft a reply to the newest enquiry', 'Prepare tomorrow\'s staff briefing']],
        ask: ['Answers come only from what the cameras logged, with the moments they come from.',
              ['How busy was it in the last hour?', 'Was anyone at the stations after closing?', 'When did the delivery arrive?']],
        design: ['Describe the work: Atlas proposes the team for it on the Team canvas (agents, tools, hand-offs). Nothing changes until you apply it.',
                 ['Answer every WhatsApp enquiry within 10 minutes', 'Email me a report of the day every evening', 'Watch the bar for long queues and tell the floor manager']],
      }[m];
      tx.placeholder = {job: 'Describe the job…', ask: 'Ask about what the cameras saw…', design: 'What work should the team be shaped for?'}[m];
      bd.innerHTML = `<div class="hint">${intro[0]}</div><div class="chips">${intro[1].map(c => `<button>${esc(c)}</button>`).join('')}</div><div class="ctx" id="sh-askctx"></div>` + (this.log[m] || []).join('');
      bd.querySelectorAll('.chips button').forEach(b => b.onclick = () => { tx.value = b.textContent; tx.focus(); });
      bd.scrollTop = 1e9;
      this.context(m);
    },
    // what the mode works on, from the desk itself: the newest jobs, the cameras as they stand, the team as it is
    async context(m) {
      const box = () => this.el && this.el.querySelector('#sh-askctx'), seq = this.ctxSeq = (this.ctxSeq || 0) + 1;
      const utc = ts => { const d = new Date(ts * 1000), t = d.toISOString().slice(11, 16); return d.toISOString().slice(0, 10) === new Date().toISOString().slice(0, 10) ? t : d.getUTCDate() + ' ' + 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ')[d.getUTCMonth()]; };
      const row = (a, b, c, cls, t) => `<div class="r"><span class="a" title="${esc(a)}">${esc(a)}</span>${b ? `<span class="b ${cls || ''}">${esc(b)}</span>` : ''}${c ? `<span class="${t ? 't' : 'c'}">${esc(c)}</span>` : ''}</div>`;   // t: c is a time (never cut)
      let head = '', rows = [], more = '';
      if (m === 'job') {
        const runs = await api('/runs'); if (seq !== this.ctxSeq) return;
        const R = Array.isArray(runs) ? runs.slice(0, 6) : null;
        head = 'Recent jobs · UTC'; more = `<a href="/desk#runs">Run history ›</a>`;
        rows = R == null ? [`<div class="r none">The jobs could not be read.</div>`] : R.length ? R.map(r => row(String(r.task || '').replace(/^Run workflow /, ''), r.active ? 'running' : /error|fail/.test(r.status || '') ? 'failed' : (r.status || 'done'), utc(r.created), r.active ? 'on' : /error|fail/.test(r.status || '') ? 'bad' : '', true)) : [`<div class="r none">No jobs yet: the first one you give lands here.</div>`];
      } else if (m === 'ask') {
        head = 'Cameras · newest note UTC'; more = `<a href="/desk#cameras">Cameras ›</a>`;
        const C = S.cameras || [];
        rows = C.length ? C.slice(0, 8).map(c => { const st = c.status || {}; return row(c.name, st.problem ? 'fix feed' : st.state === 'live' ? 'live' : st.watching ? 'starting' : 'not watched', c.last_note && c.last_note.ts ? utc(c.last_note.ts) : 'no note', st.problem ? 'warn' : st.state === 'live' ? 'ok' : '', true); })
          : [`<div class="r none">No cameras on this desk yet: answers come only from what cameras log.</div>`];
      } else {
        head = 'The team now'; more = `<a href="/desk#team">Team canvas ›</a>`;
        const A = (S.agents || []).filter(a => a.enabled !== false);
        rows = A.length ? A.slice(0, 10).map(a => row(a.name, a.id === 'atlas' ? 'lead' : '', a.id === 'atlas' ? 'Orchestrator' : (a.role || ''))) : [`<div class="r none">Only Atlas so far.</div>`];
      }
      const b = box(); if (b) b.innerHTML = `<div class="g"><span>${esc(head)}</span>${more}</div>${rows.join('')}`;
    },
    log: {job: [], ask: [], design: []},
    say(cls, html) { const s = `<div class="msg ${cls}">${html}</div>`; this.log[this.mode].push(s); const bd = this.el.querySelector('#sh-askbd'); bd.insertAdjacentHTML('beforeend', s); bd.scrollTop = 1e9; return bd.lastElementChild; },
    async send() {
      const tx = this.el.querySelector('#sh-asktx'), btn = this.el.querySelector('#sh-asksend'); const t = tx.value.trim(); if (!t) return;
      tx.value = ''; this.say('me', esc(t)); btn.disabled = true;
      try {
        if (this.mode === 'job') {
          const r = await api('/runs', {method: 'POST', body: {task: t, mode: 'auto'}});
          if (!r || r.error) this.say('sys', r && r.limited ? window.atlasLimit.html(r) : esc((r && r.error) || 'Could not start the job.'));
          else { const m = this.say('at', `On it. Atlas is planning the job and briefing the team.<br><a href="/desk#live">Watch it live</a> · <a href="/desk#runs">Run history</a>`); this.watch(r.run_id || r.id, m); refreshBadges(); }
        } else if (this.mode === 'ask') {
          const m = this.say('sys', 'Reading the camera journal…');
          const r = await api('/vision/ask', {method: 'POST', body: {question: t, hours: 24}});
          m.className = 'msg at'; m.innerHTML = r && !r.error ? esc(r.answer || 'Nothing in the journal answers that yet.') + (r.evidence && r.evidence.length ? `<br><small style="color:var(--sh-muted)">from ${r.evidence.length} logged moment${r.evidence.length === 1 ? '' : 's'} · <a href="/desk#cameras">Cameras</a></small>` : '')
            : r && r.limited ? window.atlasLimit.html(r) : esc((r && r.error) || 'Could not read the journal.');
        } else {
          this.say('at', `Opening the team canvas: Atlas proposes the team for this, and nothing changes until you apply it…`);
          setTimeout(() => { this.close(); pal.go({href: '/desk#team', after: 'teamDesignFor:' + encodeURIComponent(t)}); }, 600);
        }
      } finally { btn.disabled = false; }
    },
    async watch(id, el) {
      if (!id) return;
      for (let i = 0; i < 120; i++) {
        await new Promise(r => setTimeout(r, 3000));
        const r = await api('/runs/' + id); if (!r || r.error) return;
        const st = r.status || (r.run && r.run.status) || '';
        if (/done|completed|finished|error|failed|limit/i.test(st)) {
          const pend = (r.actions || []).filter(a => a.status === 'pending').length;
          const lim = /limit/i.test(st), why = lim ? String(r.summary || '').replace(/^Stopped:\s*/, '').split('\n')[0] : '';
          el.insertAdjacentHTML('beforeend', `<br><b>${lim ? 'Stopped at a limit: ' + esc(why) : /error|failed/i.test(st) ? 'Stopped: ' + esc(st) : 'Done.'}</b>${pend ? ` ${pend} message${pend === 1 ? '' : 's'} waiting for <a href="/desk#approvals">your approval</a>.` : ''}${lim ? ' <a href="/desk/usage">See usage</a> ·' : ''} <a href="/desk#runs">See the result</a>`);
          refreshBadges(); return;
        }
      }
    },
    open(mode, text) {
      if (!this.el) this.build();
      this.setMode(mode || this.mode);
      this.el.classList.add('on');
      const tx = this.el.querySelector('#sh-asktx'); if (text) tx.value = text; setTimeout(() => tx.focus(), 250);
    },
    close() { if (this.el) this.el.classList.remove('on'); },
  };

  // phones: a top bar with the menu, the desk name and Ask Atlas; the rail slides over the page
  let mbarWired = false;
  function mbar() {
    const m = document.createElement('div'); m.id = 'sh-mbar';
    m.innerHTML = `<button id="sh-burger" title="Menu" aria-label="Menu"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button><b id="sh-mdesk">${SETUP ? 'New desk' : 'Atlas'}</b><button class="ask" id="sh-mask">Ask Atlas</button>`;
    document.body.append(m);
    m.querySelector('#sh-burger').onclick = e => { e.stopPropagation(); document.body.classList.toggle('sh-open'); };
    m.querySelector('#sh-mask').onclick = () => askAtlas();
    if (!mbarWired) { mbarWired = true; document.addEventListener('click', e => { if (document.body.classList.contains('sh-open') && (!e.target.closest('#sh-rail') || e.target.closest('.sh-item'))) document.body.classList.remove('sh-open'); }); }
  }

  // ---------------------------------------------------------------- boot
  let keysWired = false, polling = false;
  function mount(first) {
    rail(); mbar();
    if (SETUP) { loadMe(); refreshBadges(); if (first) runAfter(); }   // an action asked for on another page (e.g. the template form) still runs
    else { loadMeta(); refreshBadges(); if (first) runAfter(); }
    if (!polling) { polling = true; setInterval(() => { if (!document.hidden) refreshBadges(); }, 12000); }
    if (!keysWired) {
      keysWired = true;
      document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !location.pathname.startsWith('/desk/ops')) { e.preventDefault(); pal.el ? pal.close() : pal.open(); }
        else if (e.key === 'Escape') { pal.close(); ask.close(); closeMenus(); }
      });
    }
  }
  async function boot() {
    const d = await api('/desks');
    if (!d || !Array.isArray(d.desks)) return;             // signed out: no frame
    SETUP = !d.desks.length;                               // a brand-new owner gets the frame with only the way in
    mount(true);
  }
  // after the first desk is built on this page: the reduced rail becomes the full one, without a reload
  async function remount() {
    const d = await api('/desks'); if (!d || !Array.isArray(d.desks)) return;
    document.querySelectorAll('#sh-rail, #sh-mbar, .sh-menu').forEach(e => e.remove());
    pal.close();
    SETUP = !d.desks.length;
    mount(false);
  }
  window.shell = {ask, pal, askAtlas, remount, refresh: refreshBadges, go: (href, after) => pal.go({href, after}), setup: () => SETUP};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
