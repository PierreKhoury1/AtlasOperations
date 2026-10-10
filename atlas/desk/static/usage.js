/* Atlas Usage page (usage spec §6). Reads GET /api/usage every 30 s while the tab is visible (and on Refresh / R),
   and /api/usage/events for older rows and the Refused / Errors tabs. Every number is the server's: the ledger, the
   counters, the provider's own figures, or a labelled formula. Text that comes from the ledger (desk and camera
   names, model ids, notes) reaches the page through textContent only; no inline handlers. */
'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const NS = 'http://www.w3.org/2000/svg';
  const DESK_HREF = /^\/desk(?:[/?#][A-Za-z0-9_\-/=&?#.]*)?$/;
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  // ---------------------------------------------------------------- DOM: elements with text, never parsed HTML
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = String(v);
      else if (k === 'title') e.title = String(v);
      else e.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of kids.flat()) if (c != null && c !== false && c !== '') e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return e;
  }
  function icon(id, cls) {
    const s = document.createElementNS(NS, 'svg'), u = document.createElementNS(NS, 'use');
    s.setAttribute('class', 'i' + (cls ? ' ' + cls : '')); s.setAttribute('aria-hidden', 'true'); u.setAttribute('href', '#' + id); s.append(u);
    return s;
  }
  const th = (text, cls) => h('th', {class: cls || null, text});
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k) || ''; localStorage.setItem(k, v); } catch (_) {} return ''; };

  // ---------------------------------------------------------------- numbers and times (UTC, labelled; local time in tooltips)
  const pad = n => String(n).padStart(2, '0');
  const now = () => Date.now() / 1000;
  function utc(ts) {
    const d = new Date(ts * 1000);
    return {hm: pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()), hms: pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()),
            dm: d.getUTCDate() + ' ' + MON[d.getUTCMonth()], mon: MON[d.getUTCMonth()], y: d.getUTCFullYear(), day: Math.floor(ts / 86400)};
  }
  function localTitle(ts) {
    try { return new Date(ts * 1000).toLocaleString([], {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) + ' your time'; }
    catch (_) { return ''; }
  }
  function dur(s) {
    s = Math.max(0, Math.ceil(s));
    if (s < 60) return s + ' s';
    const m = Math.floor(s / 60); if (m < 60) return m + ' min';
    const hh = Math.floor(m / 60), mm = m % 60; if (hh < 48) return mm ? hh + ' h ' + mm + ' min' : hh + ' h';
    const d = Math.floor(hh / 24), rh = hh % 24; return rh ? d + ' d ' + rh + ' h' : d + ' d';
  }
  function when(ts, sec) {                                     // "17:31" today, "9 Oct 17:31" before (UTC)
    const t = utc(ts), today = utc(now()).day === t.day;
    return (today ? '' : t.dm + ' ') + (sec ? t.hms : t.hm);
  }
  const fullUtc = ts => { const t = utc(ts); return `${t.dm} ${t.y}, ${t.hm} UTC`; };
  const fmtInt = n => Math.round(Number(n || 0)).toLocaleString('en-GB');
  const kfmt = n => { n = Number(n || 0); return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(Math.round(n)); };
  function money(v) { if (v == null) return '—'; v = Number(v); return '$' + (v > 0 && v < 0.01 ? v.toFixed(4) : v.toFixed(2)); }
  function mbPair(u, l) {
    const big = Math.max(Number(u) || 0, Number(l) || 0) >= 1024;
    const f = x => { x = Number(x) || 0; if (big) return (x / 1024).toFixed(1); return x >= 100 ? String(Math.round(x)) : x.toFixed(1); };
    return [f(u), l == null ? null : f(l), big ? 'GB' : 'MB'];
  }
  function bytes(n) { n = Number(n || 0); return n >= 1 << 30 ? (n / (1 << 30)).toFixed(1) + ' GB' : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B'; }
  const pctText = p => p == null ? '—' : (p >= 10 || p === 0 ? Math.round(p) : p.toFixed(1)) + '%';
  function valText(v, unit) {
    if (v == null) return '—';
    if (unit === 'usd') return money(v);
    if (unit === 'MB') { const [a, , u] = mbPair(v, v); return a + ' ' + u; }
    return fmtInt(v);
  }
  function pairText(l) {                                       // "112 / 250", "$0.42 / $2.50", "1.2 / 4.0 GB"
    if (l.unit === 'MB') { const [a, b, u] = mbPair(l.used, l.limit); return b == null ? `${a} ${u}` : `${a} / ${b} ${u}`; }
    if (l.limit == null) return valText(l.used, l.unit);
    return `${valText(l.used, l.unit)} / ${valText(l.limit, l.unit)}`;
  }
  function resetOf(l) {
    const n = now();
    if (l.resets_at) {
      const r = utc(l.resets_at);
      if (l.window === 'utc_month') return {text: r.dm, short: r.dm, title: `00:00 UTC on ${r.dm} · in ${dur(l.resets_at - n)} · ${localTitle(l.resets_at)}`};
      return {text: r.hm + ' UTC', short: r.hm + ' UTC', title: `in ${dur(l.resets_at - n)} · ${localTitle(l.resets_at)}`};
    }
    const fixed = {runs_at_once: ['when one ends', 'A run that finishes frees its place'], run_requests: ['per run', 'Each run or link study starts at 0'],
                   storage_mb: ['—', 'A running total: deleting recordings frees it'], disk_floor: ['—', 'Measured at each upload']}[l.id];
    return fixed ? {text: fixed[0], short: fixed[0] === '—' ? '' : fixed[0], title: fixed[1]} : {text: '—', short: '', title: ''};
  }

  // ---------------------------------------------------------------- state and fetching
  const S = {data: null, desk: store('usage-desk'), tab: 'all', ev: [], next: null, paged: false, open: new Set(), det: new Map(),
             loading: false, loadedAt: 0, evSeq: 0};
  async function getJSON(path) {
    const r = await fetch(path, {credentials: 'same-origin', headers: {Accept: 'application/json'}});
    if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); throw new Error('signed out'); }
    let j = null; try { j = await r.json(); } catch (_) {}
    if (!r.ok) { const e = new Error((j && (j.message || j.error)) || 'HTTP ' + r.status); e.status = r.status; throw e; }
    return j;
  }
  function q(path, params) {
    const u = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) if (v !== '' && v != null) u.set(k, v);
    const s = u.toString(); return path + (s ? '?' + s : '');
  }
  async function load() {
    if (S.loading) return;
    S.loading = true; $('#u-refresh').disabled = true;
    try {
      const d = await getJSON(q('/api/usage', {desk: S.desk}));
      S.data = d; S.loadedAt = Date.now();
      $('#u-err').classList.add('hide');
      render(d);
    } catch (e) {
      if (e.status === 404 && S.desk) { S.desk = ''; store('usage-desk', ''); S.loading = false; return load(); }   // that desk is gone
      if (e.message !== 'signed out') { $('#u-err-t').textContent = 'Could not load usage' + (e.status ? ` (HTTP ${e.status})` : '') + '.'; $('#u-err').classList.remove('hide'); }
    } finally {
      S.loading = false; $('#u-refresh').disabled = false; $('#up').setAttribute('aria-busy', 'false');
    }
  }

  // ---------------------------------------------------------------- render
  function render(d) {
    renderHead(d); renderBanners(d); renderStats(d); renderLimits(d); renderToday(d); renderCams(d);
    if (S.tab === 'all' && !S.paged) { S.ev = d.events || []; S.next = d.events_next || null; renderEvents(); }
    const c = d.event_counts || {};
    $('#n-refused').textContent = c.refused ? fmtInt(c.refused) : '';
    $('#n-error').textContent = c.error ? fmtInt(c.error) : '';
    renderKeys(d); renderWhat(d);
    $('#u-upd').textContent = 'Updated ' + utc(now()).hms + ' UTC';
  }

  function renderHead(d) {
    const w = d.windows || {}, n = now();
    const sub = $('#u-sub'); sub.textContent = '';
    if (w.day && w.month) {
      const day = utc(w.day.start), mon = utc(w.month.resets_at);
      sub.append(`Today, ${day.dm} (UTC) · day resets `, h('b', {text: '00:00 UTC', title: localTitle(w.day.resets_at)}),
                 ` (in ${dur(w.day.resets_at - n)}) · month resets `, h('b', {text: mon.dm, title: `00:00 UTC on ${mon.dm} · ${localTitle(w.month.resets_at)}`}));
    }
    const sel = $('#u-desk'), desks = d.desks || [];
    const opts = [['', 'All desks'], ...desks.map(x => [String(x.id), x.name])];
    const sig = JSON.stringify(opts);
    if (sel.dataset.sig !== sig) { sel.textContent = ''; opts.forEach(([v, t]) => sel.append(h('option', {value: v, text: t}))); sel.dataset.sig = sig; }
    sel.value = d.desk_filter ? String(d.desk_filter) : '';
    sel.closest('.sel').classList.toggle('hide', desks.length < 2 && !d.desk_filter);
  }

  function renderBanners(d) {
    const box = $('#u-banners'); box.textContent = '';
    const add = (cls, ic, ...parts) => box.append(h('div', {class: 'banner ' + cls, role: cls === 'bad' ? 'alert' : null}, icon(ic), h('span', null, ...parts)));
    const pool = d.pool || {};
    if (pool.state === 'full')
      add('bad', 'i-alert', h('b', {text: 'Shared free-model pool full. '}), `Every account's free AI requests are paused until 00:00 UTC (in ${dur((pool.resets_at || now()) - now())}). Work on your own model key is not affected.`);
    if (d.enforced === false)
      add('warn', 'i-alert', h('b', {text: 'Meter only. '}), 'Every request is counted and no Atlas limit refuses work (USAGE_ENFORCE=0). The providers’ own limits still apply.');
    if (d.mode === 'demo')
      add('info', 'i-info', h('b', {text: 'Demo mode: '}), 'desk agents and the designer are scripted and make no model calls. Live view, video, object checks, search embeddings, link studies through Hermes and the website demo still run on Atlas’s shared account; they count below and their limits apply.');
    if (d.hermes && d.hermes.mode === 'unmetered') add('warn', 'i-alert', d.hermes.note);
    if (d.owner && d.owner.unlimited) add('info', 'i-info', h('b', {text: 'No Atlas limit on this account (operator). '}), 'Requests are still counted, and the shared pool and Atlas’s paid cap still apply.');
    if (d.open) add('info', 'i-info', h('b', {text: 'Open test mode: '}), 'one shared allowance (owner 0).');
    const wait = (d.deferred || {}).waiting || 0;
    if (wait) add('info', 'i-info', h('b', {text: `${fmtInt(wait)} lead${wait === 1 ? '' : 's'} waiting. `}), 'Their runs were held at a limit; Atlas starts them once it resets. Nothing was dropped.');
  }

  // ---------------------------------------------------------------- stat strip
  const lim = (d, id) => (d.limits || []).find(l => l.id === id && !l.desk_id) || null;
  const bindLabel = b => b.id === 'pool_free_day' ? 'shared pool full' : b.id === 'house_paid_usd_month' ? 'Atlas’s paid cap reached' : b.id === 'ai_requests_day' ? 'daily allowance full' : b.label;
  function statOf(label, l, idle) {
    if (!l) return {label, v: ['–'], s: ['not reported'], cls: ''};
    if (l.limit == null) return {label, v: [valText(l.used, l.unit)], s: ['no Atlas limit'], cls: ''};
    const parts = l.unit === 'MB' ? (() => { const [a, b, u] = mbPair(l.used, l.limit); return [a, ` / ${b} ${u}`]; })() : [valText(l.used, l.unit), ' / ' + valText(l.limit, l.unit)];
    const r = resetOf(l);
    if (l.binding) return {label, v: parts, s: [h('b', {text: 'Paused'}), ` · ${bindLabel(l.binding)} · ${utc(l.binding.resets_at).hm} UTC`], cls: 'bad', title: (l.basis || {}).text};
    if (l.state === 'full') return {label, v: parts, s: [h('b', {text: 'Full'}), r.short ? ' · ' + (l.id === 'runs_at_once' ? 'new runs wait' : 'resets ' + r.short) : ''], cls: 'bad'};
    const sub = idle || (l.pct != null ? `${pctText(l.pct)}${r.short ? ' · resets ' + r.short : ''}` : r.short);
    return {label, v: parts, s: [sub], cls: l.state === 'warn' ? 'warn' : ''};
  }
  function renderStats(d) {
    const box = $('#u-stats'); box.textContent = '';
    const mon = d.windows && d.windows.month ? utc(d.windows.month.start).mon : '';
    const sends = d.sends || {}, sentParts = Object.entries(sends.by_channel || {}).map(([k, n]) => `${fmtInt(n)} ${k}`);
    if (sends.held_today) sentParts.push(`${fmtInt(sends.held_today)} held (24 h window)`);
    const cells = [statOf('AI requests', lim(d, 'ai_requests_day')), statOf('On its own', lim(d, 'ai_requests_auto_day')),
      statOf('Paid · ' + mon, lim(d, 'paid_usd_month')), statOf('Runs now', lim(d, 'runs_at_once'), 'at the same time'),
      {label: 'Sent today', v: [fmtInt(sends.today)], s: [sentParts.length ? sentParts.join(' · ') : 'nothing sent'], cls: ''},
      statOf('Files', lim(d, 'storage_mb'))];
    for (const c of cells) {
      box.append(h('div', {class: 'kpi' + (c.cls ? ' ' + c.cls : ''), title: c.title || null},
        h('span', {class: 'l', text: c.label}),
        h('span', {class: 'v'}, c.v[0], c.v[1] ? h('small', {text: c.v[1]}) : null),
        h('span', {class: 's'}, ...c.s)));
    }
  }

  // ---------------------------------------------------------------- limits table
  const KIND = {derived: 'Share of the provider’s pool', decision: 'Operator setting', doc: 'Provider’s documented limit', rule: 'Channel rule',
                override: 'Set for your account', code: 'Code default'};
  const pop = {el: null, btn: null, pinned: false};
  function openPop(btn, b, pin) {
    const el = $('#u-pop');
    if (pop.btn && pop.btn !== btn) pop.btn.setAttribute('aria-expanded', 'false');
    el.textContent = ''; el.append(h('span', {class: 'k', text: KIND[b.kind] || 'Basis'}), b.text);
    el.hidden = false; pop.btn = btn; pop.pinned = !!pin; btn.setAttribute('aria-expanded', 'true');
    const r = btn.getBoundingClientRect(), w = Math.min(340, innerWidth - 24);
    el.style.maxWidth = w + 'px';
    const left = Math.max(12, Math.min(r.right - el.offsetWidth, innerWidth - el.offsetWidth - 12));
    const below = r.bottom + 6 + el.offsetHeight <= innerHeight - 8;
    el.style.left = left + 'px'; el.style.top = (below ? r.bottom + 6 : Math.max(8, r.top - el.offsetHeight - 6)) + 'px';
  }
  function closePop(btn, force) {
    if (!pop.btn || (btn && pop.btn !== btn) || (pop.pinned && !force)) return;
    pop.btn.setAttribute('aria-expanded', 'false'); pop.btn = null; pop.pinned = false; $('#u-pop').hidden = true;
  }
  function infoBtn(b, label) {
    if (!b || !b.text) return null;
    const btn = h('button', {class: 'ib', type: 'button', 'aria-label': 'Basis: ' + label, 'aria-expanded': 'false', 'aria-describedby': 'u-pop'}, icon('i-info'));
    btn.addEventListener('click', e => { e.stopPropagation(); openPop(btn, b, true); });
    btn.addEventListener('focus', () => openPop(btn, b));
    btn.addEventListener('blur', () => closePop(btn, true));
    btn.addEventListener('mouseenter', () => { if (!pop.pinned) openPop(btn, b); });
    btn.addEventListener('mouseleave', () => { if (document.activeElement !== btn) closePop(btn); });
    return btn;
  }
  function marks(l, d) {
    const m = h('div', {class: 'marks'});
    if (l.binding) m.append(h('span', {class: 'mk bad', text: 'paused: ' + bindLabel(l.binding)}));
    if (l.refused) m.append(h('span', {class: 'mk bad', text: `refused ${fmtInt(l.refused)}×`, title: 'Refusals in this window, from the ledger'}));
    if (l.override) m.append(h('span', {class: 'mk acc', text: l.override.value == null ? 'no Atlas limit for you' : 'set for your account', title: l.override.note || ''}));
    else if (l.limit == null && ['ai_requests_day', 'ai_requests_auto_day', 'paid_usd_month', 'runs_at_once'].includes(l.id) && d.owner && d.owner.unlimited) m.append(h('span', {class: 'mk acc', text: 'no Atlas limit (operator)'}));
    if (l.id === 'paid_usd_month' && l.estimated) m.append(h('span', {class: 'mk', text: `incl. ${money(l.estimated)} estimated`, title: 'Atlas computed this part from the operator’s price table; the provider reported no cost for it'}));
    if (l.id === 'sms_segments_day' && l.state === 'off') m.append(h('span', {class: 'mk', text: 'no SMS connector'}));
    if (l.id === 'disk_floor' && l.free_mb != null) { const [f, , u] = mbPair(l.free_mb, l.free_mb); m.append(h('span', {class: 'mk' + (l.state === 'full' ? ' bad' : ''), text: `${f} ${u} free` + (l.state === 'off' ? ' · no floor set' : ` · uploads keep ${l.floor_pct}% free`)})); }
    if (l.id === 'pool_free_day' && l.state === 'full') m.append(h('span', {class: 'mk bad', text: 'every account’s free AI requests are paused until 00:00 UTC'}));
    if (l.id === 'pool_free_day' && d.operator && d.pool && d.pool.used_here != null) {
      const p = d.pool;
      m.append(h('span', {class: 'mk', text: `provider ${p.used_provider == null ? '–' : fmtInt(p.used_provider)} · here ${fmtInt(p.used_here)} · elsewhere ${p.elsewhere == null ? '–' : fmtInt(p.elsewhere)}`,
        title: 'Operator view. Provider = OpenRouter’s own count at the last read; here = this server’s requests today; elsewhere = other servers on this key (local servers, Hermes before the proxy)'}));
    }
    return m;
  }
  function barNode(l) {
    if (l.used == null || l.limit == null) return null;
    const max = Number(l.limit), v = Number(l.used), p = max > 0 ? Math.min(100, 100 * v / max) : 100;
    const cls = l.state === 'off' ? 'off' : l.state === 'full' ? 'full' : l.state === 'warn' ? 'warn' : '';
    const b = h('div', {class: 'bar' + (cls ? ' ' + cls : ''), role: 'meter', 'aria-label': l.label + (l.desk ? ' · ' + l.desk : ''),
      'aria-valuemin': '0', 'aria-valuemax': String(Math.max(0, max)), 'aria-valuenow': String(Math.max(0, Math.min(v, max))), 'aria-valuetext': pairText(l) + (l.state === 'full' ? ', full' : '')});
    const f = h('i'); f.style.width = p.toFixed(1) + '%'; b.append(f);
    return b;
  }
  function valueNode(l, d) {
    if (l.id === 'pool_free_day' && (l.used == null || !d.operator)) return h('span', {class: 'state ' + (l.state === 'full' ? 'full' : 'ok'), text: l.state === 'full' ? 'Full' : 'OK'});
    if (l.id === 'run_requests') return l.limit == null ? h('span', {class: 'of', text: 'no cap'}) : h('span', null, h('span', {class: 'of', text: 'max '}), h('b', {text: fmtInt(l.limit)}));
    if (l.used == null) return h('span', {class: 'of', text: '—'});
    if (l.limit == null) return h('span', null, h('b', {text: valText(l.used, l.unit)}), h('span', {class: 'of', text: ' · no limit'}));
    if (l.unit === 'MB') { const [a, b, u] = mbPair(l.used, l.limit); return h('span', null, h('b', {text: a}), h('span', {class: 'of', text: ` / ${b} ${u}`})); }
    return h('span', null, h('b', {text: valText(l.used, l.unit)}), h('span', {class: 'of', text: ' / ' + valText(l.limit, l.unit)}));
  }
  function limitRow(l, d, opt) {
    opt = opt || {};
    const st = l.state || 'off', r = resetOf(l), label = opt.label || l.label;
    return h('tr', {class: ['lim', st, opt.sub ? 'sub' : ''].join(' ').trim(), 'data-id': l.id},
      h('td', {class: 'lab'}, h('b', {text: label}), l.desk ? h('span', {class: 'dk', text: ' · ' + l.desk}) : null, marks(l, d)),
      h('td', {class: 'val n'}, valueNode(l, d)),
      h('td', {class: 'barc'}, barNode(l)),
      h('td', {class: 'pct n', text: pctText(l.pct), 'data-reset': r.short ? '· ' + (l.window === 'utc_month' ? 'resets ' : '') + r.short : ''}),
      h('td', {class: 'rst', text: r.text, title: r.title || null}),
      h('td', {class: 'bas'}, infoBtn(l.basis, label)),
      h('td', {class: 'basis-t', text: (l.basis || {}).text || ''}));
  }
  function histRow(d) {
    const H = d.history || []; if (!H.length || !H.some(x => x.you || x.auto)) return null;
    const max = Math.max(1, ...H.map(x => Math.max(x.you + x.auto, x.limit || 0)));
    const W = 300, HH = 34, bw = W / H.length;
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${HH}`); svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'AI requests on the shared account, last 30 UTC days: yours and Atlas’s own, with each day’s limit');
    const add = (tag, a) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); svg.append(e); return e; };
    H.forEach((x, i) => {
      const x0 = i * bw + bw * 0.15, w = bw * 0.7, tot = x.you + x.auto;
      const ha = HH * x.auto / max, hy = HH * x.you / max;
      const g = document.createElementNS(NS, 'g'); svg.append(g);
      const t = document.createElementNS(NS, 'title');
      t.textContent = `${utc(x.day).dm}: ${fmtInt(x.you)} yours, ${fmtInt(x.auto)} on its own` + (x.limit != null ? `, limit ${fmtInt(x.limit)}` : ', no limit on record');
      g.append(t);
      const r = (cls, y, hgt) => { const e = document.createElementNS(NS, 'rect'); e.setAttribute('class', cls); e.setAttribute('x', x0.toFixed(2)); e.setAttribute('y', y.toFixed(2)); e.setAttribute('width', w.toFixed(2)); e.setAttribute('height', Math.max(0, hgt).toFixed(2)); g.append(e); };
      if (!tot) r('hb-z', HH - 1, 1);
      else { r('hb-a', HH - ha, ha); r('hb-y', HH - ha - hy, hy); }
      if (x.limit != null) add('line', {class: 'hb-l', x1: (i * bw).toFixed(2), x2: ((i + 1) * bw).toFixed(2), y1: (HH - HH * x.limit / max).toFixed(2), y2: (HH - HH * x.limit / max).toFixed(2), 'vector-effect': 'non-scaling-stroke'});
    });
    const lg = h('div', {class: 'lg'}, h('span', null, 'last 30 days'), h('span', null, h('i', {class: 'y'}), 'you'), h('span', null, h('i', {class: 'a'}), 'on its own'), h('span', null, h('i', {class: 'ln'}), 'limit'));
    return h('tr', {class: 'hist'}, h('td', {colspan: '6'}, h('div', {class: 'hist-w'}, svg, lg)));
  }
  function ruleRow(r) {
    const cap = c => ({whatsapp: 'WhatsApp', instagram: 'Instagram'}[c] || c);
    const names = (r.channels || []).length ? r.channels.map(cap).join(' and ') : 'WhatsApp and Instagram';
    const t = h('td', {class: 'rtext', colspan: '5'}, h('b', {text: names + ': '}), 'Atlas sends only within 24 h of the person’s last message (Meta rule). Held today: ',
      h('b', {class: 'mono', text: fmtInt(r.held_today)}), '.');
    if (!(r.channels || []).length) t.append(h('span', {class: 'rnote', text: 'No WhatsApp or Instagram connected on these desks.'}));
    if (r.alerts_on_whatsapp) t.append(h('span', {class: 'rnote', text: 'Camera alerts by WhatsApp reach you only if you messaged this desk’s WhatsApp number in the last 24 h. SMS, email and Slack have no such window.'}));
    return h('tr', {class: 'rule'}, t, h('td', {class: 'bas'}, infoBtn(r.basis, r.label)));
  }
  function renderLimits(d) {
    const box = $('#u-limits'); box.textContent = '';
    const L = d.limits || [];
    const tb = h('tbody');
    const group = text => tb.append(h('tr', {class: 'gh'}, h('td', {colspan: '6', text})));
    group('AI on Atlas’s shared account');
    const l1 = lim(d, 'ai_requests_day');
    if (l1) { tb.append(limitRow(l1, d)); const hr = histRow(d); if (hr) tb.append(hr); }
    const l2 = lim(d, 'ai_requests_auto_day'); if (l2) tb.append(limitRow(l2, d, {sub: true, label: 'of which on its own'}));
    ['paid_usd_month', 'run_requests', 'runs_at_once'].forEach(id => { const l = lim(d, id); if (l) tb.append(limitRow(l, d)); });
    group('Your channels');
    (d.rules || []).forEach(r => tb.append(ruleRow(r)));
    L.filter(l => l.group === 'channels').forEach(l => tb.append(limitRow(l, d)));
    group('Storage');
    L.filter(l => l.group === 'storage').forEach(l => tb.append(limitRow(l, d)));
    group('Shared pool, all accounts');
    L.filter(l => l.group === 'pool').forEach(l => tb.append(limitRow(l, d, {label: 'Free-model pool today'})));
    box.append(h('table', {class: 'tbl lt'}, h('thead', null, h('tr', null, th('Limit'), th('Used / limit', 'n'), h('th', {'aria-label': 'Bar'}), th('%', 'n'), th('Resets'), th('Basis', 'n'))), tb));
    const full = L.filter(l => l.state === 'full' && l.group !== 'pool').length, warn = L.filter(l => l.state === 'warn').length;
    $('#lim-meta').textContent = full ? `${full} at the limit${warn ? ` · ${warn} past 80%` : ''}` : warn ? `${warn} past 80%` : 'all below 80%';
  }

  // ---------------------------------------------------------------- where today's requests went
  const ACT_UNIT = {'design.say': ['msg', 'msgs'], 'design.study': ['study', 'studies'], 'run.you': ['run', 'runs'], 'run.auto': ['run', 'runs'],
    'camera.note': ['note', 'notes'], 'camera.rollup': ['summary', 'summaries'], 'ask.cameras': ['ask', 'asks'], 'camera.look': ['look', 'looks'],
    'camera.detect': ['tick', 'ticks'], 'camera.describe': ['question', 'questions'], 'objects.follow': ['check', 'checks'], 'objects.call': ['check', 'checks'],
    'objects.ask': ['question', 'questions'], 'theatre.narrate': ['narration', 'narrations'], 'video.describe': ['video', 'videos'], 'team.design': ['design', 'designs']};
  const actText = (s, n) => n == null ? '—' : fmtInt(n) + (ACT_UNIT[s] ? ' ' + ACT_UNIT[s][n === 1 ? 0 : 1] : '');
  function costText(r) {
    const parts = [];
    if (r.cost_usd != null) parts.push(money(r.cost_usd));
    if (r.est_usd != null) parts.push(`est. ${money(r.est_usd)}`);
    if (r.cost_unreported) parts.push(`${fmtInt(r.cost_unreported)} not reported`);
    return parts.length ? parts.join(' + ') : '—';
  }
  const tokText = r => (r.tokens_in || r.tokens_out) ? `${kfmt(r.tokens_in)} / ${kfmt(r.tokens_out)}` : '—';
  function pacedText(r) {
    const p = r.paced || {}, k = Object.keys(p)[0]; if (!k) return '';
    const m = Math.max(1, Math.round(p[k] / 60));
    return r.surface === 'camera.detect' ? `one every ${m} min` : `one note every ${m} min`;
  }
  function renderToday(d) {
    const box = $('#u-today'); box.textContent = '';
    if (d.windows && d.windows.day) $('#today-meta').textContent = utc(d.windows.day.start).dm + ' · UTC day';
    const rows = d.today || [];
    if (!rows.length) {
      box.append(h('div', {class: 'none'}, h('b', {text: 'Nothing counted yet today.'}), ` Counting since ${fullUtc(d.counting_since || now())}.`));
      return;
    }
    const tb = h('tbody');
    for (const r of rows) {
      const sub = r.key === 'byo' ? 'not limited by Atlas' : r.key === 'demo' ? 'no model call' : '';
      tb.append(h('tr', null,
        h('td', {class: 'what'}, h('b', {text: r.label}), sub ? h('small', {text: sub}) : null),
        h('td', {class: 'n', 'data-l': 'actions', text: actText(r.surface, r.actions)}),
        h('td', {class: 'n', 'data-l': 'requests', text: fmtInt(r.requests)}),
        h('td', {class: 'n' + (r.embed_requests ? '' : ' zero'), 'data-l': 'embeddings', text: r.embed_requests ? fmtInt(r.embed_requests) : '—'}),
        h('td', {class: 'n' + ((r.tokens_in || r.tokens_out) ? '' : ' zero'), 'data-l': 'tokens', text: tokText(r)}),
        h('td', {class: 'n', 'data-l': 'cost', text: costText(r)}),
        h('td', {class: 'paced', text: pacedText(r)})));
      if (r.hermes_requests) tb.append(h('tr', {class: 'sub'}, h('td', {class: 'what', text: 'of which Hermes agent steps'}), h('td', {class: 'n zero'}),
        h('td', {class: 'n', 'data-l': 'requests', text: fmtInt(r.hermes_requests)}), h('td', {class: 'n zero'}), h('td', {class: 'n zero'}), h('td', {class: 'n zero'}), h('td', {class: 'paced'})));
    }
    box.append(h('table', {class: 'tbl tt'}, h('thead', null, h('tr', null, th('What'), th('Actions', 'n'), th('Requests', 'n'), th('Embed', 'n'), th('Tokens in / out', 'n'), th('Cost', 'n'), th('Paced'))), tb));
  }
  function renderCams(d) {
    const box = $('#u-cams'); box.textContent = '';
    const cams = d.cameras || []; if (!cams.length) return;
    const many = (d.desks || []).length > 1;
    const tb = h('tbody');
    for (const c of cams) {
      let count, ccls = '';
      if (c.detector === 'local') count = 'Local detector: no AI requests';
      else if (c.detect === 'paused') { count = c.detect_status || 'AI counts paused'; ccls = 'paused'; }
      else if (c.detect === 'paced') { count = c.detect_status || 'AI counts paced'; ccls = 'paced'; }
      else count = c.detect_gap_s >= 60 ? `AI counts: one every ${Math.round(c.detect_gap_s / 60)} min at most` : 'AI counts on each moving tick';
      let jr, jcls = '';
      if (!c.journal) jr = 'Journal off';
      else if (/paused/i.test(c.journal_status || '')) { jr = c.journal_status; jcls = 'paused'; }
      else if (c.journal_gap_s) { jr = `Paced: one note every ${Math.max(1, Math.round(c.journal_gap_s / 60))} min (set for one at least every ${c.journal_every_s} s)`; jcls = 'paced'; }
      else jr = `A note at least every ${c.journal_every_s} s`;
      const name = h('td', null, h('b', {text: c.name}), many ? h('span', {class: 'muted', text: ' · ' + c.desk}) : null, c.watching ? null : h('span', {class: 'muted', text: ' · not watched'}));
      if (c.detector !== 'local' && !c.alert_on_motion) name.append(h('small', {class: 'cnote', text: 'Without a local detector, count rules need AI requests; with ‘alert on motion’ off it does not alert while counting is paused.'}));
      tb.append(h('tr', null, name, h('td', {class: 'st ' + ccls, 'data-l': 'counting', text: count}), h('td', {class: 'st ' + jcls, 'data-l': 'journal', text: jr}),
        h('td', {class: 'n', 'data-l': 'last tick', text: c.last_tick ? when(c.last_tick) : '—', title: c.last_tick ? fullUtc(c.last_tick) : null})));
    }
    box.append(h('div', {class: 'cams-t'}, h('div', {class: 'gk', text: `Cameras · ${cams.length}`}),
      h('table', {class: 'tbl'}, h('thead', null, h('tr', null, th('Camera'), th('Counting'), th('Journal'), th('Last tick', 'n'))), tb)));
  }

  // ---------------------------------------------------------------- recent usage
  const ST_TEXT = {ok: 'ok', error: 'error', refused: 'refused', held: 'held', not_sent: 'not sent'};
  function reqText(it) {
    if (it.requests) return fmtInt(it.requests);
    if (it.unit === 'messages') return fmtInt(it.units) + ' msg';
    if (it.unit === 'segments') return fmtInt(it.units) + ' seg';
    if (it.unit === 'bytes') return bytes(it.units);
    if (it.unit === 'embeddings') return fmtInt(it.units) + ' emb';
    return '—';
  }
  function evSub(it) {
    if (it.status === 'refused' || it.status === 'held') return it.note || '';
    if (it.key === 'demo') return 'scripted agents: no model call';
    const p = [];
    if (it.models && it.models.length) p.push(it.models[0] + (it.models.length > 1 ? ` +${it.models.length - 1}` : ''));
    if (it.hermes_requests) p.push(`${fmtInt(it.hermes_requests)} Hermes steps`);
    if (it.embed_requests && it.requests) p.push(`${fmtInt(it.embed_requests)} embeddings`);
    if (it.key === 'byo') p.push('your key');
    return p.join(' · ');
  }
  function evRow(it) {
    const open = S.open.has(it.id);
    const tr = h('tr', {class: 'ev' + (open ? ' open' : ''), tabindex: '0', 'aria-expanded': open ? 'true' : 'false', title: 'Show each request'},
      h('td', {class: 't mono', text: when(it.ts, true), title: fullUtc(it.ts) + ' · ' + localTitle(it.ts)}),
      h('td', {class: 'dk', text: it.desk || '—'}),
      h('td', {class: 'what'}, icon('i-chev', 'chev'), h('b', {text: it.what}), it.count > 1 ? h('span', {class: 'cnt', text: '×' + fmtInt(it.count)}) : null,
        evSub(it) ? h('small', {text: evSub(it)}) : null),
      h('td', {class: 'n req', text: reqText(it)}),
      h('td', {class: 'n tok', text: tokText(it)}),
      h('td', {class: 'n cost', text: costText(it)}),
      h('td', {class: 'stc'}, h('span', {class: 'st ' + it.status, text: it.status === 'refused' && it.lane === 'auto' ? 'paused' : (ST_TEXT[it.status] || it.status)})));
    const go = () => toggle(it);
    tr.addEventListener('click', go);
    tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    return tr;
  }
  function detRow(it) {
    const td = h('td', {colspan: '7'});
    const det = S.det.get(it.id);
    if (!det) td.append(h('div', {class: 'none', text: 'Reading the ledger rows…'}));
    else if (det.error) td.append(h('div', {class: 'none', text: 'Could not read these rows: ' + det.error}));
    else {
      const tb = h('tbody');
      det.rows.slice(0, 100).forEach(r => tb.append(h('tr', null,
        h('td', {class: 'mono', text: when(r.ts, true), title: fullUtc(r.ts)}),
        h('td', null, r.what, r.kind !== 'request' ? h('span', {class: 'muted', text: ' · ' + r.kind}) : null, r.note ? h('div', {class: 'muted', text: r.note}) : null),
        h('td', {class: 'm', text: r.model || r.provider || '—'}),
        h('td', null, h('span', {class: 'st ' + (r.status === 'provider_limit' ? 'error' : r.status), text: r.status === 'provider_limit' ? 'provider limit' : (ST_TEXT[r.status] || r.status)})),
        h('td', {class: 'n', text: r.http_status ? String(r.http_status) : '—'}),
        h('td', {class: 'n', text: (r.tokens_in || r.tokens_out) ? `${kfmt(r.tokens_in)} / ${kfmt(r.tokens_out)}${r.tokens_cached ? ` (${kfmt(r.tokens_cached)} cached)` : ''}` : '—'}),
        h('td', {class: 'n', text: r.cost_usd != null ? money(r.cost_usd) : r.est_usd != null ? `est. ${money(r.est_usd)}` : (r.kind === 'request' && r.pool !== 'demo' ? 'not reported' : '—'), title: r.cost_basis || null}))));
      td.append(h('table', {class: 'det-t'}, h('thead', null, h('tr', null, th('Time UTC'), th('What'), th('Model'), th('Status'), th('HTTP', 'n'), th('Tokens in / out', 'n'), th('Cost', 'n'))), tb));
      const more = det.rows.length - 100;
      const links = (det.links || []).filter(l => DESK_HREF.test(String(l.href || '')));
      if (more > 0 || links.length) td.append(h('div', {class: 'det-links'}, more > 0 ? h('span', {class: 'muted', text: `+${fmtInt(more)} more rows`}) : null,
        ...links.map(l => h('a', {href: l.href, text: l.label}))));
    }
    return h('tr', {class: 'det'}, td);
  }
  async function toggle(it) {
    if (S.open.has(it.id)) { S.open.delete(it.id); renderEvents(); return; }
    S.open.add(it.id); renderEvents();
    if (S.det.has(it.id)) return;
    try {
      const r = await getJSON(q('/api/usage/detail', {ids: (it.ids || [it.id]).join(',')}));
      S.det.set(it.id, {rows: r.rows || [], links: r.links || []});
    } catch (e) { S.det.set(it.id, {error: e.message}); }
    renderEvents();
  }
  function renderEvents() {
    const box = $('#u-events'), keep = document.activeElement && document.activeElement.closest && document.activeElement.closest('#u-events tr.ev');
    const keepIdx = keep ? [...box.querySelectorAll('tr.ev')].indexOf(keep) : -1;
    box.textContent = '';
    if (!S.ev.length) {
      box.append(h('div', {class: 'none', text: S.tab === 'refused' ? 'Nothing refused in the ledger (it keeps 35 days).' : S.tab === 'error' ? 'No errors in the ledger (it keeps 35 days).' : 'Nothing counted yet.'}));
    } else {
      const tb = h('tbody');
      S.ev.forEach(it => { tb.append(evRow(it)); if (S.open.has(it.id)) tb.append(detRow(it)); });
      box.append(h('table', {class: 'tbl et'}, h('thead', null, h('tr', null, th('Time UTC'), th('Desk'), th('What'), th('Req', 'n'), th('Tokens', 'n'), th('Cost', 'n'), th('Status'))), tb));
      if (keepIdx >= 0) { const rows = box.querySelectorAll('tr.ev'); if (rows[keepIdx]) rows[keepIdx].focus({preventScroll: true}); }
    }
    $('#u-older').classList.toggle('hide', !S.next);
    $('#ev-meta').textContent = S.ev.length ? `${fmtInt(S.ev.length)} shown · newest first · grouped by action` : '';
  }
  async function loadEvents(append) {
    const seq = ++S.evSeq;
    const params = {status: S.tab === 'all' ? '' : S.tab, desk: S.desk, cursor: append ? S.next : ''};
    try {
      const r = await getJSON(q('/api/usage/events', params));
      if (seq !== S.evSeq) return;
      S.ev = append ? S.ev.concat(r.events || []) : (r.events || []);
      S.next = r.next || null; S.paged = append || S.tab !== 'all';
      renderEvents();
    } catch (e) {
      if (seq === S.evSeq) { $('#ev-meta').textContent = 'Could not read older rows: ' + e.message; }
    }
  }
  function setTab(tab) {
    S.tab = tab; S.paged = tab !== 'all'; S.next = null;
    document.querySelectorAll('.utabs button').forEach(b => { const on = b.dataset.tab === tab; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false'); });
    if (tab === 'all' && S.data) { S.ev = S.data.events || []; S.next = S.data.events_next || null; S.paged = false; renderEvents(); }
    else loadEvents(false);
  }

  // ---------------------------------------------------------------- model keys and what counts
  function renderKeys(d) {
    const box = $('#u-keys'); box.textContent = '';
    const list = h('div', {class: 'keys'});
    list.append(h('div', {class: 'krow'}, icon('i-gauge'), h('div', null, h('b', {text: 'Atlas’s shared account: '}),
      'design chat, link studies, camera vision and search always run here, even when a desk has its own key. They count in your AI requests.')));
    if (d.hermes && d.hermes.note) list.append(h('div', {class: 'krow'}, icon('i-info'), h('div', {text: d.hermes.note})));
    const byo = d.byo || [];
    for (const k of byo) {
      const lt = k.last_test;
      const tested = lt && lt.ts ? `tested ${when(lt.ts)} UTC, ${lt.ok ? 'ok' : 'failed'}` : 'not tested yet';
      const t = k.today || {};
      const line = h('div', null, h('b', {text: k.desk}), ` · ${k.label} (your key ${k.key_hint || 'set'})${k.default ? ' · default' : ''} · ${tested} · `,
        h('span', {class: 'mono', text: fmtInt(t.requests)}), ` request${t.requests === 1 ? '' : 's'} today on your key`
          + (t.cost_usd != null ? ` · ${money(t.cost_usd)} reported` : '') + ' · not limited by Atlas');
      const v = k.provider_view;
      if (v && v.seen_at) line.append(h('small', {text: `Your key: ${money(v.usage_monthly)} this month${v.limit != null ? ` of ${money(v.limit)}` : ''} (OpenRouter, read ${when(v.seen_at)} UTC)`}));
      else if (v && v.error) line.append(h('small', {text: `OpenRouter did not give this key’s figures (${v.error}).`}));
      else if (v) line.append(h('small', {text: 'Reading OpenRouter’s figures for this key…'}));
      else if (k.provider_note) line.append(h('small', {text: k.provider_note}));
      list.append(h('div', {class: 'krow'}, icon('i-key'), line));
    }
    if (!byo.length) list.append(h('div', {class: 'krow'}, icon('i-key'), h('div', null, 'No desk runs on its own key yet. ',
      h('a', {href: '/desk#setup', text: 'Add one in Desk setup'}), ': that desk’s agents then run on your account, metered here and not limited by Atlas except the per-run cap.')));
    box.append(list);
  }
  function renderWhat(d) {
    const box = $('#u-what'); box.textContent = '';
    const w = h('div', {class: 'wc'},
      h('p', {text: 'An AI request is one call to a model. A design message uses 1–2 when nothing fails, up to 4 with retries (6 before the designer change). A run uses one per agent step, plus its Hermes specialists’ own steps. A camera note uses 2 (note and check). A camera without a local detector uses 1 per moving tick to count objects.'}),
      h('p', {text: 'Costs are what the provider reported for each request; anything Atlas computes says “estimate” and its source. Requests on your own key are counted here and never limited by Atlas, except by the per-run cap. Times are UTC.'}));
    const dl = h('dl'), seen = new Set();
    for (const l of (d.limits || []).concat(d.rules || [])) {
      if (!l.basis || !l.basis.text || seen.has(l.id)) continue;
      seen.add(l.id);
      dl.append(h('dt', {text: l.id === 'pool_free_day' ? 'Free-model pool today' : l.label}), h('dd', {text: l.basis.text}));
    }
    w.append(dl);
    box.append(w);
  }

  // ---------------------------------------------------------------- wiring
  function themeBtn() {
    const dark = document.documentElement.dataset.theme === 'dark', b = $('#u-theme');
    b.title = dark ? 'Light theme' : 'Dark theme'; b.setAttribute('aria-label', dark ? 'Switch to the light theme' : 'Switch to the dark theme');
    b.querySelector('use').setAttribute('href', dark ? '#i-sun' : '#i-moon');
  }
  $('#u-theme').addEventListener('click', () => {
    const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = t; store('atlas-theme', t); themeBtn();
  });
  new MutationObserver(themeBtn).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});   // the frame's own toggle
  themeBtn();
  $('#u-refresh').addEventListener('click', () => { load(); if (S.tab !== 'all' || S.paged) loadEvents(false); });
  $('#u-retry').addEventListener('click', () => load());
  $('#u-desk').addEventListener('change', e => {
    S.desk = e.target.value; store('usage-desk', S.desk); S.open.clear(); S.det.clear(); S.paged = false;
    load(); if (S.tab !== 'all') loadEvents(false);
  });
  document.querySelectorAll('.utabs button').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  $('#u-older').addEventListener('click', () => loadEvents(true));
  document.addEventListener('click', e => { if (pop.btn && !e.target.closest('#u-pop') && !e.target.closest('.ib')) closePop(null, true); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closePop(null, true);
    const typing = e.target && (e.target.closest('input,textarea,select,[contenteditable]') || e.target.closest('#sh-pal,#sh-ask'));
    if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 'r' || e.key === 'R')) { e.preventDefault(); load(); }
  });
  window.addEventListener('scroll', () => closePop(null, true), {passive: true});
  window.addEventListener('resize', () => closePop(null, true));
  setInterval(() => { if (!document.hidden) load(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && Date.now() - S.loadedAt > 30000) load(); });
  setTimeout(() => { if (!document.getElementById('sh-rail')) document.body.classList.add('no-frame'); }, 3000);
  load();
})();
