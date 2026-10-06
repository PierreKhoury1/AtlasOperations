/* Atlas app frame: the same navigation, search (Ctrl K) and Ask Atlas panel on every page of the desk.
   Each page keeps its own content; this adds the frame around it. Pages opt in with shell.css + this script. */
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
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
  };
  const svg = k => `<svg viewBox="0 0 24 24">${IC[k] || ''}</svg>`;
  const NAV = [
    ['Work', [['home', 'Home', '/desk#dash', 'home'], ['cases', 'Cases', '/desk/ops', 'inbox'], ['approvals', 'Approvals', '/desk#approvals', 'check'],
              ['live', 'Live runs', '/desk#live', 'pulse'], ['leads', 'Leads', '/desk#leads', 'leads'], ['crm', 'Contacts', '/desk#crm', 'db']]],
    ['See', [['cameras', 'Cameras', '/desk#cameras', 'camera'], ['objects', 'Objects', '/desk/objects', 'grid'], ['review', 'Review', '/desk/review', 'film'],
             ['security', 'Security', '/desk/cyber', 'shield']]],
    ['Team', [['team', 'Agents', '/desk#team', 'team'], ['design', 'Atlas workspace', '/desk/workspace', 'spark'], ['automations', 'Automations', '/desk#automations', 'clock'],
              ['runs', 'Run history', '/desk#runs', 'list']]],
    ['Connect', [['integrations', 'Integrations', '/desk#integrations', 'link'], ['audit', 'Audit log', '/desk#audit', 'doc'], ['report', 'Monthly report', '/desk#report', 'chart'],
                 ['setup', 'Desk setup', '/desk#setup', 'gear']]],
  ];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  async function api(path, opt = {}) {
    const r = await fetch('/api' + path, {method: opt.method || 'GET', headers: {'Content-Type': 'application/json'}, credentials: 'same-origin',
                                           body: opt.body ? JSON.stringify(opt.body) : undefined});
    if (r.status === 401) return null;
    try { return await r.json(); } catch (e) { return null; }
  }
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} return null; };
  const isIndex = () => location.pathname === '/desk' || location.pathname === '/desk/';
  function current() {
    const p = location.pathname.replace(/\/$/, '');
    if (p === '/desk/ops') return 'cases';
    if (p === '/desk/objects') return 'objects';
    if (p === '/desk/review') return 'review';
    if (p === '/desk/cyber' || p === '/desk/static/cyber.html') return 'security';
    if (p === '/desk/workspace') return 'design';
    const h = location.hash.slice(1) || 'dash';
    return h === 'dash' ? 'home' : h;
  }
  const S = {desks: [], cur: null, me: null, stats: null, cams: 0, agents: [], cameras: [], cases: []};

  // ---------------------------------------------------------------- rail
  function rail() {
    const r = document.createElement('div'); r.id = 'sh-rail'; r.setAttribute('role', 'navigation');   // not <nav>: the pages style bare nav elements
    r.innerHTML = `<div class="sh-top"><a class="sh-brand" href="/desk#dash" data-go="dash"><i></i><span>Atlas</span></a>
        <button class="sh-iconbtn" id="sh-mini" title="Collapse the menu">${svg('side')}</button></div>
      <button class="sh-desk" id="sh-desk" title="Switch desk">${svg('home')}<span class="nm"><b id="sh-dname">…</b><small id="sh-dsub">loading</small></span>${svg('chev')}</button>
      <div class="sh-actions"><button class="sh-act ask" id="sh-askbtn" title="Give the team a job or ask the cameras">${svg('chat')}<span>Ask Atlas</span></button>
        <button class="sh-act" id="sh-search" title="Search and jump anywhere">${svg('search')}<span>Search</span>${location.pathname.startsWith('/desk/ops') ? '' : '<kbd>Ctrl K</kbd>'}</button></div>
      <div class="sh-nav">${NAV.map(([g, items]) => `<div class="sh-grp">${g}</div>` + items.map(([id, label, href, ic]) =>
        `<a class="sh-item" data-id="${id}" href="${href}" title="${esc(label)}">${svg(ic)}<span class="lbl">${esc(label)}</span><span class="sh-badge" id="sh-b-${id}" hidden></span></a>`).join('')).join('')}</div>
      <div class="sh-foot"><div class="sh-av" id="sh-av">?</div><div class="sh-who"><b id="sh-me">…</b><small id="sh-co"></small></div>
        <button class="sh-iconbtn" id="sh-acct" title="Account">${svg('gear')}</button></div>`;
    document.body.prepend(r);
    document.body.classList.add('sh-on');
    if (store('sh-mini') === '1') document.body.classList.add('sh-mini');
    r.querySelector('#sh-mini').onclick = () => { const m = document.body.classList.toggle('sh-mini'); store('sh-mini', m ? '1' : '0'); window.dispatchEvent(new Event('resize')); };
    r.querySelector('#sh-askbtn').onclick = () => ask.open();
    r.querySelector('#sh-search').onclick = () => pal.open();
    r.querySelector('#sh-desk').onclick = e => deskMenu(e.currentTarget);
    r.querySelector('#sh-acct').onclick = e => acctMenu(e.currentTarget);
    r.addEventListener('click', e => {                       // on the main desk page, its own sections switch without a reload
      const a = e.target.closest('a[href^="/desk#"]'); if (!a || !isIndex() || typeof window.go !== 'function') return;
      e.preventDefault(); const sec = a.getAttribute('href').split('#')[1]; window.go(sec); mark();
    });
    mark();
    window.addEventListener('hashchange', mark);
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
  async function refreshBadges() {
    const s = await api('/stats'); if (!s || s.needs_desk) return; S.stats = s;
    badge('approvals', s.pending || 0, 'hot');
    badge('cases', (s.cases && s.cases.you) || 0, 'hot');
    badge('live', s.active_runs || 0, 'live');
    badge('leads', s.leads || 0);
  }
  function menu(anchor, html) {
    document.querySelectorAll('.sh-menu').forEach(m => m.remove());
    const m = document.createElement('div'); m.className = 'sh-menu'; m.innerHTML = html;
    const r = anchor.getBoundingClientRect(); m.style.left = Math.min(r.left, innerWidth - 250) + 'px';
    document.body.append(m);
    const top = r.bottom + 6 + m.offsetHeight > innerHeight ? r.top - m.offsetHeight - 6 : r.bottom + 6;
    m.style.top = Math.max(8, top) + 'px';
    setTimeout(() => document.addEventListener('click', function h(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('click', h); } }), 0);
    return m;
  }
  function deskMenu(btn) {
    const m = menu(btn, S.desks.map(d => `<button data-d="${d.id}" class="${d.id === S.cur ? 'cur' : ''}">${svg('home')}${esc(d.business_name || d.name)}${d.id === S.cur ? ' ✓' : ''}</button>`).join('')
      + `<hr><a href="/desk/workspace?new=1">${svg('plus')}New desk with Atlas</a><a href="/desk#setup">${svg('gear')}Desk setup</a>`);
    m.querySelectorAll('[data-d]').forEach(b => b.onclick = async () => {
      if (+b.dataset.d === S.cur) return m.remove();
      await api(`/desks/${b.dataset.d}/select`, {method: 'POST', body: {}}); location.href = '/desk#dash'; location.reload();
    });
  }
  function acctMenu(btn) {
    const dark = document.documentElement.dataset.theme !== 'light';
    const m = menu(btn, `<button id="sh-th">${svg('moon')}${dark ? 'Light theme' : 'Dark theme'}</button><a href="/desk#setup">${svg('gear')}Desk setup</a><a href="/">${svg('home')}Website</a><hr><a href="/logout">${svg('out')}Log out</a>`);
    m.querySelector('#sh-th').onclick = () => {
      const t = dark ? 'light' : 'dark'; document.documentElement.dataset.theme = t; store('atlas-theme', t);
      if (typeof window.themeLabel === 'function') window.themeLabel(); m.remove();
    };
  }
  async function loadMeta() {
    const [d, me, cfg, cams, cases] = await Promise.all([api('/desks'), api('/me'), api('/config'), api('/cameras'), api('/cases?open=1&limit=50')]);
    if (d && d.desks) {
      S.desks = d.desks; S.cur = d.current;
      const cur = d.desks.find(x => x.id === d.current) || d.desks[0];
      if (cur) document.getElementById('sh-dname').textContent = cur.business_name || cur.name;
      { const md = document.getElementById('sh-mdesk'); if (md && cur) md.textContent = cur.business_name || cur.name; }
      const ws = document.querySelector('#sh-rail .sh-item[data-id="design"]'); if (ws && cur) ws.href = '/desk/workspace?desk=' + cur.id;   // this desk's agents, cameras and chat
    }
    if (cfg && cfg.agents) S.agents = cfg.agents;
    if (cams && cams.cameras) { S.cameras = cams.cameras; badge('cameras', cams.cameras.length, cams.cameras.some(c => c.watch_job && c.watch_job.enabled) ? 'live' : ''); }
    if (cases && cases.cases) S.cases = cases.cases;
    const sub = []; if (S.agents.length) sub.push(`${S.agents.length} agent${S.agents.length === 1 ? '' : 's'}`); if (S.cameras.length) sub.push(`${S.cameras.length} camera${S.cameras.length === 1 ? '' : 's'}`);
    document.getElementById('sh-dsub').textContent = sub.join(' · ') || (cfg && cfg.mode === 'live' ? 'live' : 'desk');
    if (me && me.id) {
      document.getElementById('sh-me').textContent = me.name || me.email; document.getElementById('sh-co').textContent = me.email || '';
      document.getElementById('sh-av').textContent = (me.name || me.email || '?').trim()[0].toUpperCase();
    } else { document.getElementById('sh-me').textContent = 'Guest'; document.getElementById('sh-co').textContent = 'open mode'; }
  }

  // ---------------------------------------------------------------- palette (Ctrl K)
  const pal = {
    el: null, items: [], sel: 0,
    build() {
      const nav = NAV.flatMap(([g, items]) => items.map(([id, label, href, ic]) => ({g: 'Go to', label, ic, href, kw: g})));
      const act = [
        {g: 'Do', label: 'Give the team a job', ic: 'chat', run: () => ask.open('job')},
        {g: 'Do', label: 'Ask the cameras a question', ic: 'camera', run: () => ask.open('ask')},
        {g: 'Do', label: 'New case', ic: 'inbox', href: '/desk/ops', after: 'newCase'},
        {g: 'Do', label: 'Add a camera', ic: 'camera', href: '/desk#cameras'},
        {g: 'Do', label: 'Add an agent', ic: 'team', href: '/desk#team', after: 'teamAddReady'},
        {g: 'Do', label: 'Connect an app (email, WhatsApp, calendar…)', ic: 'link', href: '/desk#integrations'},
        {g: 'Do', label: 'Change the team with Atlas', ic: 'spark', run: () => ask.open('design')},
        {g: 'Do', label: 'Start a new desk with Atlas', ic: 'spark', href: '/desk/workspace?new=1'},
      ];
      const ag = S.agents.filter(a => a.id !== 'atlas').map(a => ({g: 'Agents', label: a.name, hint: a.role, ic: 'team', href: '/desk#team', after: 'teamOpenId:' + a.id}));
      const cm = S.cameras.map(c => ({g: 'Cameras', label: c.name, hint: (c.seen && c.seen.counts) ? Object.entries(c.seen.counts).map(([k, n]) => `${n} ${k}`).join(', ') : c.source_kind, ic: 'camera', href: '/desk#cameras', after: 'openLive:' + c.id}));
      const cs = S.cases.map(c => ({g: 'Open cases', label: c.title || ('Case ' + c.id), hint: c.state || c.type, ic: 'inbox', href: '/desk/ops#case/' + c.id}));
      const dk = S.desks.filter(d => d.id !== S.cur).map(d => ({g: 'Switch desk', label: d.business_name || d.name, ic: 'home', run: async () => { await api(`/desks/${d.id}/select`, {method: 'POST', body: {}}); location.href = '/desk#dash'; location.reload(); }}));
      return [...act, ...nav, ...ag, ...cm, ...cs, ...dk];
    },
    open() {
      if (this.el) return;
      const el = this.el = document.createElement('div'); el.id = 'sh-pal';
      el.innerHTML = `<div class="box"><input placeholder="Search pages, agents, cameras, cases, or type what you want to do…" autocomplete="off"><div class="list"></div>
        <div class="ft"><span>↑↓ choose</span><span>Enter open</span><span>Esc close</span></div></div>`;
      document.body.append(el);
      const inp = el.querySelector('input'); this.all = this.build(); this.render('');
      inp.oninput = () => { this.sel = 0; this.render(inp.value); };
      inp.onkeydown = e => {
        if (e.key === 'ArrowDown') { e.preventDefault(); this.sel = Math.min(this.sel + 1, this.items.length - 1); this.render(inp.value, true); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); this.sel = Math.max(this.sel - 1, 0); this.render(inp.value, true); }
        else if (e.key === 'Enter') { e.preventDefault(); if (this.items[this.sel]) this.go(this.items[this.sel]); else if (inp.value.trim()) { const t = inp.value.trim(); this.close(); ask.open('job', t); } }
        else if (e.key === 'Escape') this.close();
      };
      el.onclick = e => { if (e.target === el) this.close(); };
      inp.focus();
    },
    render(q, keep) {
      const t = q.trim().toLowerCase();
      const score = it => { const s = (it.label + ' ' + (it.hint || '') + ' ' + it.g + ' ' + (it.kw || '')).toLowerCase(); if (!t) return 1; if (s.includes(t)) return 2 + (it.label.toLowerCase().startsWith(t) ? 1 : 0); return t.split(/\s+/).every(w => s.includes(w)) ? 1 : 0; };
      this.items = this.all.map(it => [score(it), it]).filter(([s]) => s > 0).sort((a, b) => b[0] - a[0]).map(([, it]) => it).slice(0, 40);
      if (!t) this.items = this.all.slice(0, 40);
      const list = this.el.querySelector('.list');
      if (!this.items.length) { list.innerHTML = `<div class="none">Nothing matches. Press Enter to give the team this as a job.</div>`; return; }
      let g = '';
      list.innerHTML = this.items.map((it, i) => { const head = it.g !== g ? `<div class="g">${esc(it.g)}</div>` : ''; g = it.g;
        return head + `<div class="it ${i === this.sel ? 'sel' : ''}" data-i="${i}">${svg(it.ic)}<span>${esc(it.label)}</span>${it.hint ? `<small>${esc(String(it.hint).slice(0, 60))}</small>` : ''}</div>`; }).join('');
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
      el.innerHTML = `<div class="hd"><div class="orb">A</div><div><b>Ask Atlas</b><small>your team lead: briefs the agents, checks their work</small></div><button title="Close (Esc)">×</button></div>
        <div class="tabs"><button data-m="job">Give a job</button><button data-m="ask">Ask the cameras</button><button data-m="design">Change the team</button></div>
        <div class="bd" id="sh-askbd"></div>
        <div class="cmp"><textarea id="sh-asktx" rows="2"></textarea><button class="send" id="sh-asksend">Send</button></div>
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
      bd.innerHTML = `<div class="hint">${intro[0]}</div><div class="chips">${intro[1].map(c => `<button>${esc(c)}</button>`).join('')}</div>` + (this.log[m] || []).join('');
      bd.querySelectorAll('.chips button').forEach(b => b.onclick = () => { tx.value = b.textContent; tx.focus(); });
      bd.scrollTop = 1e9;
    },
    log: {job: [], ask: [], design: []},
    say(cls, html) { const s = `<div class="msg ${cls}">${html}</div>`; this.log[this.mode].push(s); const bd = this.el.querySelector('#sh-askbd'); bd.insertAdjacentHTML('beforeend', s); bd.scrollTop = 1e9; return bd.lastElementChild; },
    async send() {
      const tx = this.el.querySelector('#sh-asktx'), btn = this.el.querySelector('#sh-asksend'); const t = tx.value.trim(); if (!t) return;
      tx.value = ''; this.say('me', esc(t)); btn.disabled = true;
      try {
        if (this.mode === 'job') {
          const r = await api('/runs', {method: 'POST', body: {task: t, mode: 'auto'}});
          if (!r || r.error) this.say('sys', esc((r && r.error) || 'Could not start the job.'));
          else { const m = this.say('at', `On it. Atlas is planning the job and briefing the team.<br><a href="/desk#live">Watch it live</a> · <a href="/desk#runs">Run history</a>`); this.watch(r.run_id || r.id, m); refreshBadges(); }
        } else if (this.mode === 'ask') {
          const m = this.say('sys', 'Reading the camera journal…');
          const r = await api('/vision/ask', {method: 'POST', body: {question: t, hours: 24}});
          m.className = 'msg at'; m.innerHTML = r && !r.error ? esc(r.answer || 'Nothing in the journal answers that yet.') + (r.evidence && r.evidence.length ? `<br><small style="color:var(--sh-muted)">from ${r.evidence.length} logged moment${r.evidence.length === 1 ? '' : 's'} · <a href="/desk#cameras">Cameras</a></small>` : '') : esc((r && r.error) || 'Could not read the journal.');
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
        if (/done|completed|finished|error|failed/i.test(st)) {
          const pend = (r.actions || []).filter(a => a.status === 'pending').length;
          el.insertAdjacentHTML('beforeend', `<br><b>${/error|failed/i.test(st) ? 'Stopped: ' + esc(st) : 'Done.'}</b>${pend ? ` ${pend} message${pend === 1 ? '' : 's'} waiting for <a href="/desk#approvals">your approval</a>.` : ''} <a href="/desk#runs">See the result</a>`);
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
  window.shell = {ask, pal, refresh: refreshBadges, go: (href, after) => pal.go({href, after})};

  // phones: a top bar with the menu, the desk name and Ask Atlas; the rail slides over the page
  function mbar() {
    const m = document.createElement('div'); m.id = 'sh-mbar';
    m.innerHTML = `<button id="sh-burger" title="Menu" aria-label="Menu"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button><b id="sh-mdesk">Atlas</b><button class="ask" id="sh-mask">Ask Atlas</button>`;
    document.body.append(m);
    m.querySelector('#sh-burger').onclick = e => { e.stopPropagation(); document.body.classList.toggle('sh-open'); };
    m.querySelector('#sh-mask').onclick = () => ask.open();
    document.addEventListener('click', e => { if (document.body.classList.contains('sh-open') && (!e.target.closest('#sh-rail') || e.target.closest('.sh-item'))) document.body.classList.remove('sh-open'); });
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    const d = await api('/desks');
    if (!d || !d.desks || !d.desks.length) return;          // signed out, or a brand-new owner still meeting Atlas: no frame yet
    rail(); mbar(); loadMeta(); refreshBadges(); runAfter();
    setInterval(() => { if (!document.hidden) refreshBadges(); }, 12000);
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !location.pathname.startsWith('/desk/ops')) { e.preventDefault(); pal.el ? pal.close() : pal.open(); }
      else if (e.key === 'Escape') { pal.close(); ask.close(); }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
