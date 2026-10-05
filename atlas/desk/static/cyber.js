/* Atlas cyber desk: the dark filming surface of a security operations desk.
   LIVE (default) polls the desk's /api/cyber/* routes. ?record=<name> is LIVE plus a recorder: every poll response that
   changed becomes a frame, saved to /api/cyber/recordings. ?replay=<name> loads one recording and draws any instant of it
   from (bundle, t) alone: no clock, no randomness, no network after loading, so a capture repeats pixel for pixel.
   Every log-derived string is attacker-controlled data. It reaches the page only through textContent (HTML and SVG);
   the static markup lives in cyber.html. Containment is only ever decided here, through the approval route. */
'use strict';

/* ================================================================== pure core (also loaded by the Node tests) */
const CyberCore = (() => {
  const POLICY_LINE = 'Every target is in the evidence.';
  const NVD_NOTICE = 'This product uses the NVD API but is not endorsed or certified by the NVD.';
  const APPROVAL_LINE = 'Containment runs only after a person approves it.';
  const SECREPO_CREDIT = 'Data: Security Repo by Mike Sconzo (secrepo.com), CC BY 4.0';
  const SEV_RANK = {info: 0, low: 1, medium: 2, high: 3, critical: 4};
  const SEV_SHORT = {info: 'INFO', low: 'LOW', medium: 'MED', high: 'HIGH', critical: 'CRIT'};
  const KINDS = ['config', 'timeline', 'graph', 'detections', 'incident', 'containment', 'ui'];
  const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;          // recording names, as the portal stores them
  const ANIM_S = 0.35;                                    // fade / slide-in of a new node, edge, row or card
  const PRESS_S = 0.25;                                   // a recorded Approve/Reject click shows pressed this long
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const p2 = n => (n < 10 ? '0' : '') + n;

  const sevRank = s => SEV_RANK[s] ?? 0;
  const fmtInt = n => String(Math.round(Number(n) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const trunc = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const num = v => (v === null || v === undefined || v === '' || !isFinite(Number(v)) ? null : Number(v));

  /* ---------------------------------------------------------------- time (UTC unless a zone is named) */
  function utc(ts) { const n = num(ts); if (n === null) return null; const d = new Date(n * 1000); return isNaN(d.getTime()) ? null : d; }
  function ymd(d) { return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}`; }
  function md(d) { return `${MON[d.getUTCMonth()]} ${p2(d.getUTCDate())}`; }
  function hms(d) { return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`; }
  function hm(d) { return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`; }
  // Same text as cyber.fmt_ts: "2012-03-17 14:41:27Z", or "Mar 17 14:41:27Z" when the log carried no year.
  function fmtTs(ts, year = true) { const d = utc(ts); return d ? `${year ? ymd(d) : md(d)} ${hms(d)}Z` : ''; }
  // Header clock (the label already says UTC).
  function fmtClock(ts, year = true) { const d = utc(ts); return d ? `${year ? ymd(d) : md(d)} ${hms(d)}` : ''; }
  // "2012-03-17 18:25 → 20:08"; a second date only when the range crosses midnight.
  function fmtRange(a, b, year = true) {
    const da = utc(a), db = utc(b);
    if (!da) return '';
    const left = `${year ? ymd(da) : md(da)} ${hm(da)}`;
    if (!db || Math.abs(Number(b) - Number(a)) < 60 && hm(da) === hm(db)) return left;
    const same = ymd(da) === ymd(db);
    return `${left} → ${same ? '' : (year ? ymd(db).slice(5) : md(db)) + ' '}${hm(db)}`;
  }
  function fmtDur(s) {
    s = Math.max(0, Math.round(Number(s) || 0));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m${s % 60 ? ' ' + (s % 60) + 's' : ''}`;
    if (s < 86400) return `${Math.floor(s / 3600)}h${Math.floor(s % 3600 / 60) ? ' ' + Math.floor(s % 3600 / 60) + 'm' : ''}`;
    return `${Math.floor(s / 86400)}d${Math.floor(s % 86400 / 3600) ? ' ' + Math.floor(s % 86400 / 3600) + 'h' : ''}`;
  }
  // HH:MM:SS of an epoch time in a named zone (decision lines: the viewer's zone, or the recording's in REPLAY).
  function clockIn(ts, tz) {
    const d = utc(ts);
    if (!d || !(Number(ts) > 0)) return '';
    for (const zone of [tz || 'UTC', 'UTC']) {
      try {
        const parts = new Intl.DateTimeFormat('en-GB', {timeZone: zone, hour: '2-digit', minute: '2-digit', second: '2-digit',
                                                        hourCycle: 'h23'}).formatToParts(d);
        const g = k => (parts.find(p => p.type === k) || {}).value || '00';
        return `${g('hour')}:${g('minute')}:${g('second')}`;
      } catch (_) { /* unknown zone: fall back to UTC */ }
    }
    return hms(d);
  }

  /* ---------------------------------------------------------------- masking (presentation only; the data stays real) */
  function v4Internal(a, b) {
    return a === 10 || a === 127 || a === 0 || a >= 224 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) ||
           (a === 169 && b === 254) || (a === 100 && b >= 64 && b <= 127);
  }
  function parseV6(s) {
    const t = s.toLowerCase(), dbl = t.indexOf('::');
    if (dbl !== t.lastIndexOf('::')) return null;
    let groups;
    if (dbl >= 0) {
      const head = t.slice(0, dbl), tail = t.slice(dbl + 2);
      const h = head ? head.split(':') : [], tl = tail ? tail.split(':') : [];
      if (h.length + tl.length > 7) return null;
      groups = h.concat(Array(8 - h.length - tl.length).fill('0'), tl);
    } else {
      groups = t.split(':');
      if (groups.length !== 8) return null;
    }
    return groups.every(g => /^[0-9a-f]{1,4}$/.test(g)) ? groups.map(g => parseInt(g, 16)) : null;
  }
  function v6Internal(g) {
    const zeroHead = g.slice(0, 7).every(x => x === 0);
    return (g[0] & 0xfe00) === 0xfc00 || (g[0] & 0xffc0) === 0xfe80 || (zeroHead && (g[7] === 0 || g[7] === 1));
  }
  const V6_RE = /(?<![\w:.])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:]|\.\d)/g;
  const V4_RE = /(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\w|\.\d)/g;
  // Public IPv4 → a.b.x.x (kept: 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, 100.64/10, 0/8, 224/3);
  // public IPv6 → g1:g2:x:x:: (kept: fc00::/7, fe80::/10, ::1, ::). Words such as Scan::Port_Scan never match.
  function maskText(s, on) {
    s = s === null || s === undefined ? '' : String(s);
    if (!on) return s;
    s = s.replace(V6_RE, m => { const g = parseV6(m); return g && !v6Internal(g) ? `${g[0].toString(16)}:${g[1].toString(16)}:x:x::` : m; });
    return s.replace(V4_RE, (m, a, b, c, d) => {
      const o = [a, b, c, d].map(Number);
      if (o.some(x => x > 255)) return m;
      return v4Internal(o[0], o[1]) ? m : `${o[0]}.${o[1]}.x.x`;
    });
  }

  /* ---------------------------------------------------------------- citations */
  // "[#12]" cites log event 12 (kind sec); "[cam #12]" cites camera event 12. Everything else stays text.
  function splitCitations(s) {
    const str = s === null || s === undefined ? '' : String(s), out = [], re = /\[(cam ?)?#(\d+)\]/g;
    let last = 0, m;
    while ((m = re.exec(str))) {
      if (m.index > last) out.push({text: str.slice(last, m.index)});
      const cam = !!m[1], id = Number(m[2]);
      out.push({cite: cam ? 'cam' : 'sec', id, label: cam ? `[cam #${id}]` : `[#${id}]`});
      last = re.lastIndex;
    }
    if (last < str.length) out.push({text: str.slice(last)});
    return out;
  }

  /* ---------------------------------------------------------------- containment card copy (the film quotes it) */
  function cardHeader(status) {
    if (status === 'pending') return 'CONTAINMENT · AWAITING APPROVAL';
    if (status === 'sent' || status === 'approved') return 'CONTAINMENT · APPROVED';
    if (status === 'rejected') return 'CONTAINMENT · REJECTED';
    if (status === 'failed') return 'CONTAINMENT · FAILED';
    return 'CONTAINMENT · ' + String(status || '').toUpperCase();
  }
  function connectorLine(a) {
    const c = String((a && a.connector) || '');
    switch (a && a.connector_status) {
      case 'ready': return `Executes via ${c} after approval`;
      case 'none': return 'No containment connector · approval records a simulated action';
      case 'missing': return `Connector ${c} not found · approval records a simulated action`;
      case 'auto_on': return `Connector ${c} runs without approval · not used · simulated`;
      case 'not_http': return `Connector ${c} is not HTTP · simulated`;
      default: return '';
    }
  }
  // "Approved · 14:02:11 · Pierre": the time is decided_at in the viewer's zone (REPLAY: the recording's zone).
  // A failed action was approved; the dispatch failed afterwards (the result line says so).
  const DECISION = {approved: 'Approved · ', sent: 'Approved · ', failed: 'Approved · ', rejected: 'Rejected · '};
  function decisionLine(a, tz) {
    const head = a && DECISION[a.status];
    if (!head) return '';
    const rest = [clockIn(a.decided_at, tz), a.decided_by ? String(a.decided_by) : ''].filter(Boolean);
    return rest.length ? head + rest.join(' · ') : head.slice(0, -3);
  }
  // The containment route answers {actions: [...], other_pending}; a bare list or another key name is read the same way.
  function actionsOf(d) {
    if (!d) return [];
    if (Array.isArray(d)) return d;
    for (const k of ['actions', 'containment', 'items']) if (Array.isArray(d[k])) return d[k];
    return [];
  }
  // Newest pending first, then the most recently decided; at most three cards.
  function orderCards(list) {
    const pend = list.filter(a => a.status === 'pending').sort((a, b) => (b.created || 0) - (a.created || 0) || b.id - a.id);
    const done = list.filter(a => a.status !== 'pending')
      .sort((a, b) => (b.decided_at || b.created || 0) - (a.decided_at || a.created || 0) || b.id - a.id);
    return pend.concat(done).slice(0, 3);
  }

  /* ---------------------------------------------------------------- recordings */
  const edgeKey = e => `e:${e.src}>${e.dst}>${e.kind}`;
  const actKey = (run, a) => `a:${run}:${a.ts}:${a.agent}:${a.kind}`;
  // Keys of everything that fades in when it first arrives (precomputed per recording, tracked live in LIVE).
  function appearKeys(kind, d) {
    const out = [];
    if (!d || typeof d !== 'object') return out;
    if (kind === 'graph') {
      for (const n of d.nodes || []) out.push('n:' + n.id);
      for (const e of d.edges || []) out.push(edgeKey(e));
    } else if (kind === 'detections') {
      for (const x of d.detections || []) out.push('d:' + x.id);
    } else if (kind === 'containment') {
      for (const a of actionsOf(d)) out.push('c:' + a.id, `c:${a.id}:${a.status}`);
    } else if (kind === 'incident' && d.run) {
      out.push('r:' + d.run.id);
      if (d.summary) out.push(`r:${d.run.id}:summary`);
      for (const a of d.activity || []) out.push(actKey(d.run.id, a));
    } else if (kind === 'timeline') {
      for (const m of d.marks || []) out.push('m:' + m.id);
      for (const s of d.spans || []) out.push('s:' + s.id);
    }
    return out;
  }
  const PREP = new WeakMap();
  function prepare(bundle) {
    let p = PREP.get(bundle);
    if (p) return p;
    const frames = (Array.isArray(bundle && bundle.frames) ? bundle.frames : [])
      .map((f, i) => ({t: Number(f && f.t) || 0, kind: f && f.kind, data: f && f.data, i}))
      .filter(f => KINDS.includes(f.kind))
      .sort((a, b) => a.t - b.t || a.i - b.i);
    const by = {};
    for (const k of KINDS) by[k] = [];
    for (const f of frames) by[f.kind].push(f);
    const first = new Map();
    for (const f of frames) for (const k of appearKeys(f.kind, f.data)) if (!first.has(k)) first.set(k, f.t);
    const clock = by.timeline.filter(f => f.data && num(f.data.last_ts) !== null).map(f => ({t: f.t, v: Number(f.data.last_ts)}));
    p = {frames, by, first, clock};
    PREP.set(bundle, p);
    return p;
  }
  function lastAt(arr, t) {                       // index of the last item with item.t <= t, or -1
    let lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid].t <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }
  // The event clock between two real observations moves linearly; after the last one it holds.
  function clockAt(clock, t) {
    const i = lastAt(clock, t);
    if (i < 0) return null;
    if (i === clock.length - 1) return clock[i].v;
    const a = clock[i], b = clock[i + 1];
    return b.t > a.t ? a.v + (b.v - a.v) * (t - a.t) / (b.t - a.t) : a.v;
  }
  // Everything the page shows at t seconds into a recording: per kind the last frame at or before t.
  function stateAt(bundle, t) {
    const p = prepare(bundle);
    t = Number(t) || 0;
    const st = {t, at: {}, ui: [], clock: clockAt(p.clock, t), first: p.first};
    for (const k of KINDS) {
      if (k === 'ui') continue;
      const i = lastAt(p.by[k], t);
      st[k] = i >= 0 ? p.by[k][i].data : null;
      st.at[k] = i >= 0 ? p.by[k][i].t : null;
    }
    st.ui = p.by.ui.slice(0, lastAt(p.by.ui, t) + 1).map(f => Object.assign({t: f.t}, f.data || {}));
    return st;
  }
  function ease(x) { x = Math.min(1, Math.max(0, x)); return 1 - (1 - x) * (1 - x) * (1 - x); }
  function appear(first, key, t) {               // 0..1 visibility of something that first arrived at first.get(key)
    const f = first && first.get(key);
    if (f === undefined || f === null) return 1;
    return ease((t - f) / ANIM_S);
  }

  /* ---------------------------------------------------------------- layout helpers */
  const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800,
                      604800, 1209600, 2592000, 7776000, 31536000];
  function niceTicks(a, b, maxTicks) {
    const span = b - a;
    if (!(span > 0) || !(maxTicks >= 1)) return {step: 0, ticks: []};
    let step = TICK_STEPS[TICK_STEPS.length - 1];
    for (const s of TICK_STEPS) if (span / s <= maxTicks) { step = s; break; }
    const ticks = [];
    for (let x = Math.ceil(a / step) * step; x <= b + 1e-6 && ticks.length < 400; x += step) ticks.push(x);
    return {step, ticks};
  }
  // Detection spans above the lanes: highest severity first, each in the first row where it does not overlap.
  function packSpans(spans, xOf, maxRows) {
    const order = spans.slice().sort((a, b) => sevRank(b.severity) - sevRank(a.severity) || a.first_ts - b.first_ts ||
                                                (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    const ends = [], out = [];
    for (const s of order) {
      const x0 = xOf(s.first_ts), x1 = Math.max(xOf(s.last_ts), x0 + 3);
      let row = ends.findIndex(e => e < x0 - 4);
      if (row < 0 && ends.length < maxRows) { row = ends.length; ends.push(-Infinity); }
      if (row < 0) row = ends.indexOf(Math.min(...ends));
      ends[row] = Math.max(ends[row], x1);
      out.push({span: s, row, x0, x1});
    }
    return out;
  }
  function nodeWeight(n) {
    const p = n.props || {};
    return Number(n.type === 'user' ? p.attempts : n.type === 'sig' ? p.count : p.events) || 0;
  }
  // Layered, deterministic: sources | signatures and users | targets | cameras. Inside a column: score desc, then id.
  function layoutGraph(graph, w, h) {
    const cols = [[], [], [], []];
    for (const n of (graph && graph.nodes) || []) {
      const p = n.props || {};
      if (n.type === 'camera') cols[3].push(n);
      else if (n.type === 'sig' || n.type === 'user') cols[1].push(n);
      else if (p.role === 'source') cols[0].push(n);
      else cols[2].push(n);
    }
    const byScore = (a, b) => (Number((b.props || {}).score) || 0) - (Number((a.props || {}).score) || 0) ||
                              (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
    const active = cols.map((c, i) => [c.sort(byScore), i]).filter(([c]) => c.length);
    const out = {};
    if (!active.length) return out;
    // room for the outer columns' labels (mono 11.5px ~ 6.9px a character, at most 28) and sub-labels (~20 characters)
    const labelPx = col => Math.max(126, col.reduce((m, n) => Math.max(m, Math.min(28, String(n.label || n.id).length)), 0) * 6.9) + 30;
    const padL = active.length > 1 ? Math.min(w * 0.3, Math.max(70, labelPx(active[0][0]))) : 0;
    const padR = Math.min(w * 0.3, Math.max(70, labelPx(active[active.length - 1][0])));
    const top = 40, bottom = 16;
    const xs = active.length === 1 ? [Math.max(16, (w - padR) / 2)]
      : active.map((_, k) => padL + k * (w - padL - padR) / (active.length - 1));
    active.forEach(([col, ci], k) => {
      const step = (h - top - bottom) / col.length;
      // horizontal room for this column's labels: to the panel edge (outer columns) or to the next column
      const room = k === 0 && active.length > 1 ? xs[0] - 4 : k === active.length - 1 ? w - xs[k] - 4 : xs[k + 1] - xs[k] - 22;
      col.forEach((n, j) => {
        const r = Math.min(Math.max(3, 2.5 + 2.4 * Math.log10(1 + nodeWeight(n))), 13, Math.max(2.5, step * 0.42));
        out[n.id] = {x: Math.round(xs[k] * 10) / 10, y: Math.round((top + step * (j + 0.5)) * 10) / 10,
                     r: Math.round(r * 10) / 10, col: ci, side: k === 0 && active.length > 1 ? 'l' : 'r',
                     label: j % Math.max(1, Math.ceil(15 / step)) === 0,    // dense column: label every k-th node
                     room: Math.max(0, Math.round(room - r - 9))};
      });
    });
    return out;
  }

  return {POLICY_LINE, NVD_NOTICE, APPROVAL_LINE, SECREPO_CREDIT, SEV_RANK, SEV_SHORT, KINDS, NAME_RE, ANIM_S, PRESS_S,
          sevRank, fmtInt, trunc, num, fmtTs, fmtClock, fmtRange, fmtDur, clockIn, maskText, splitCitations, cardHeader,
          connectorLine, decisionLine, actionsOf, orderCards, edgeKey, actKey, appearKeys, prepare, stateAt, appear,
          niceTicks, packSpans, nodeWeight, layoutGraph};
})();

if (typeof module !== 'undefined') module.exports = CyberCore;

/* ================================================================== page (browser only) */
if (typeof document !== 'undefined') (() => {
  const C = CyberCore;
  const SVGNS = 'http://www.w3.org/2000/svg';           // namespace identifier for createElementNS, never fetched
  const $ = s => document.querySelector(s);
  const Q = new URLSearchParams(location.search);
  const POLL_MS = {timeline: 2000, containment: 1500, incident: 2000, graph: 3000, detections: 3000, config: 15000};
  const ROUTE = {timeline: '/api/cyber/timeline', containment: '/api/cyber/containment', incident: '/api/cyber/incident',
                 graph: '/api/cyber/graph', detections: '/api/cyber/detections', config: '/api/cyber/config'};
  const WINDOWED = ['timeline', 'graph', 'detections'];
  const FACES = ['400 14px "IBM Plex Sans"', '500 14px "IBM Plex Sans"', '600 14px "IBM Plex Sans"',
                 '500 14px "IBM Plex Sans Condensed"', '600 14px "IBM Plex Sans Condensed"',
                 '400 14px "IBM Plex Mono"', '500 14px "IBM Plex Mono"'];

  const V = {
    mode: Q.get('replay') !== null ? 'replay' : 'live',
    tz: 'UTC', desk: {id: null, name: ''}, mask: false,
    data: {config: null, timeline: null, graph: null, detections: null, incident: null, containment: null},
    raw: {},                       // LIVE: last JSON text per kind (a poll that did not change renders nothing)
    first: new Map(),              // LIVE: key -> page clock second it first arrived
    seen: new Set(),               // LIVE: kinds that answered at least once
    animUntil: 0, rafQueued: false, t0: performance.now(),
    hl: null,                      // LIVE: {cite, id} of the clicked citation chip
    busy: {},                      // LIVE: action id -> 'approve'|'reject' while the decision is in flight
    bundle: null, stopped: false, rec: null,
  };
  const pageClock = () => (performance.now() - V.t0) / 1000;
  const M = s => C.maskText(s === null || s === undefined ? '' : s, V.mask);

  let readyResolve;
  window.__ready = new Promise(r => { readyResolve = r; });
  window.__duration = 0;

  /* ---------------------------------------------------------------- DOM helpers (text only, never markup) */
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function sv(tag, attrs, text) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) e.setAttribute(k, attrs[k]);
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function svTitle(node, text) { node.appendChild(sv('title', {}, text)); return node; }
  function sizeOf(node) {
    const r = node.getBoundingClientRect();
    return {w: Math.max(80, Math.round(r.width)), h: Math.max(60, Math.round(r.height))};
  }
  const fadeStyle = (node, a, dy) => {
    if (a >= 1) return node;
    node.style.opacity = a.toFixed(3);
    if (dy) node.style.transform = `translateY(${((1 - a) * dy).toFixed(2)}px)`;
    return node;
  };

  /* ---------------------------------------------------------------- render: everything from one state */
  function liveState() {
    const d = V.data;
    return {config: d.config, timeline: d.timeline, graph: d.graph, detections: d.detections, incident: d.incident,
            containment: d.containment, ui: [], first: V.first,
            clock: d.timeline && C.num(d.timeline.last_ts) !== null ? Number(d.timeline.last_ts) : null};
  }
  function renderAll(st, t) {
    const ctx = {t, first: st.first, ui: st.ui || [], clock: st.clock};
    if (V.mode === 'live') V.mask = Q.get('mask') === '1' || !!(st.config && st.config.mask_public_ips);
    renderHeader(st, ctx);
    renderTimeline(st, ctx);
    renderGraph(st, ctx);
    renderIncident(st, ctx);
    renderRail(st, ctx);
  }

  /* ---------------------------------------------------------------- header */
  function renderHeader(st, ctx) {
    const tl = st.timeline, lanes = (tl && tl.lanes) || [];
    $('#desk-name').textContent = M(V.desk.name || '');
    const rp = tl && Array.isArray(tl.replays) && tl.replays[0];
    $('#replay-label').textContent = rp && rp.name ? M(rp.name) : '';
    $('#clock').textContent = ctx.clock !== null && ctx.clock !== undefined ? C.fmtClock(ctx.clock, !(tl && tl.year_assumed)) : '—';
    let events = 0, high = 0;
    for (const l of lanes) {
      events += Number(l.total) || 0;
      for (const a of l.alerts || []) high += Number(a) || 0;
    }
    $('#c-events').textContent = C.fmtInt(events);
    $('#c-high').textContent = C.fmtInt(high);
    $('#c-dets').textContent = C.fmtInt(st.detections ? st.detections.total || 0 : 0);
  }

  /* ---------------------------------------------------------------- timeline */
  function windowText(tl) {
    const yr = !tl.year_assumed;
    const parts = [C.fmtRange(tl.since, tl.until, yr) + ' UTC'];
    if (tl.bins && tl.bin_s) parts.push(`${tl.bins} BINS × ${C.fmtDur(tl.bin_s).toUpperCase()}`);
    if (tl.year_assumed) parts.push('YEAR NOT IN LOG');
    return parts.join(' · ');
  }
  function tickLabel(ts, step) {
    const s = C.fmtTs(ts, true);                                  // "2012-03-17 18:25:25Z"
    if (step >= 86400) return C.fmtTs(ts, false).slice(0, 6);     // "Mar 17"
    if (s.slice(11, 19) === '00:00:00') return C.fmtTs(ts, false).slice(0, 6);
    return step < 60 ? s.slice(11, 19) : s.slice(11, 16);
  }
  function renderTimeline(st, ctx) {
    const svg = $('#tl-svg');
    svg.replaceChildren();
    const tl = st.timeline;
    const lanes = (tl && tl.lanes) || [], cams = (tl && tl.cameras) || [];
    $('#tl-window').textContent = tl && tl.until > tl.since ? windowText(tl) : '';
    $('#tl-empty').hidden = !!(lanes.length || cams.length) || !tl;
    const {w: W, h: H} = sizeOf(svg);
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    if (!tl || !(tl.until > tl.since)) return;
    const X0 = 196, X1 = W - 20, span = tl.until - tl.since;
    const xOf = ts => X0 + (Number(ts) - tl.since) / span * (X1 - X0);
    const inWin = ts => Number(ts) >= tl.since - 1e-6 && Number(ts) <= tl.until + 1e-6;

    // detection spans (severity >= medium) in up to four rows above the lanes
    const spans = (tl.spans || []).filter(s => C.sevRank(s.severity) >= 2 && C.num(s.first_ts) !== null);
    const packed = C.packSpans(spans, ts => Math.min(X1, Math.max(X0, xOf(ts))), 4);
    const rows = packed.reduce((m, p) => Math.max(m, p.row + 1), 0);
    const spanTop = 18, lanesTop = spanTop + Math.max(1, rows) * 8 + 12, axisH = 30;
    const camHead = cams.length ? 26 : 0, gap = 8;
    const all = lanes.map(l => ({l, cam: false})).concat(cams.map(c => ({l: c, cam: true})));
    const avail = H - lanesTop - axisH - camHead;
    let laneH = all.length ? (avail - gap * (all.length - 1)) / all.length : 0;      // lanes fill the panel
    let shown = all;
    if (laneH < 24 && all.length) {                                // too many lanes: keep the first that fit
      const fit = Math.max(1, Math.floor((avail + gap) / (24 + gap)));
      shown = all.slice(0, fit);
      laneH = (avail - gap * (shown.length - 1)) / shown.length;
    }
    const lanesBottom = lanesTop + shown.length * laneH + (shown.length - 1) * gap + (shown.some(x => x.cam) ? camHead : 0);

    // grid and axis
    const ticks = C.niceTicks(tl.since, tl.until, Math.max(2, Math.floor((X1 - X0) / 120)));
    const hasCursor = ctx.clock !== null && ctx.clock !== undefined && inWin(ctx.clock);
    const tagW = 62, tagX = hasCursor ? Math.min(X1 - tagW / 2, Math.max(X0 + tagW / 2, xOf(ctx.clock))) : null;
    const grid = sv('g', {class: 'tl-grid'});
    for (const x of ticks.ticks) {
      const px = xOf(x);
      grid.appendChild(sv('line', {x1: px, x2: px, y1: lanesTop - 6, y2: lanesBottom + 4}));
      if (tagX !== null && Math.abs(px - tagX) < tagW / 2 + 24) continue;      // the cursor tag covers this label
      grid.appendChild(sv('text', {x: px, y: lanesBottom + 20, class: 'tl-tick', 'text-anchor': 'middle'}, tickLabel(x, ticks.step)));
    }
    grid.appendChild(sv('line', {x1: X0, x2: X1, y1: lanesBottom + 4, y2: lanesBottom + 4, class: 'tl-axis'}));
    svg.appendChild(grid);

    // spans
    const gs = sv('g', {class: 'tl-spans'});
    gs.appendChild(sv('text', {x: X0 - 14, y: spanTop + Math.max(1, rows) * 4 + 4, class: 'tl-side', 'text-anchor': 'end'}, 'DETECTIONS'));
    for (const p of packed) {
      const a = C.appear(ctx.first, 's:' + p.span.id, ctx.t);
      if (a <= 0) continue;
      const y = spanTop + p.row * 8 + 2;
      const line = sv('line', {x1: p.x0, x2: p.x1, y1: y, y2: y, class: 'span sev-' + p.span.severity, opacity: a < 1 ? a.toFixed(3) : null});
      svTitle(line, M(p.span.title || p.span.id));
      gs.appendChild(line);
      if (p.span.severity === 'critical') {
        gs.appendChild(sv('circle', {cx: p.x0, cy: y, r: 2.6, class: 'span-cap', opacity: a < 1 ? a.toFixed(3) : null}));
        gs.appendChild(sv('circle', {cx: p.x1, cy: y, r: 2.6, class: 'span-cap', opacity: a < 1 ? a.toFixed(3) : null}));
      }
    }
    svg.appendChild(gs);

    // lanes
    const laneY = {};
    const gl = sv('g', {class: 'tl-lanes'});
    let y = lanesTop, camLabelDone = false;
    shown.forEach(({l, cam}, i) => {
      if (cam && !camLabelDone) {
        camLabelDone = true;
        gl.appendChild(sv('text', {x: 22, y: y + 15, class: 'tl-group'}, 'CAMERAS'));
        gl.appendChild(sv('line', {x1: 100, x2: X1, y1: y + 11, y2: y + 11, class: 'tl-groupline'}));
        y += camHead;
      }
      const counts = l.counts || [], alerts = l.alerts || [], bins = counts.length || tl.bins || 1;
      const bw = (X1 - X0) / bins, base = y + laneH;
      let mx = 0;
      for (const c of counts) mx = Math.max(mx, Number(c) || 0);
      gl.appendChild(sv('rect', {x: X0, y, width: X1 - X0, height: laneH, class: 'lane-bg'}));
      gl.appendChild(sv('line', {x1: X0, x2: X1, y1: base, y2: base, class: 'lane-base'}));
      const name = cam ? l.camera : l.sensor;
      gl.appendChild(sv('text', {x: 22, y: y + laneH / 2 - 1, class: 'lane-name'}, C.trunc(M(name || '?'), 22)));
      const sub = (cam ? 'CAMERA' : String(l.source || '').toUpperCase()) + ' · ' + C.fmtInt(l.total || 0);
      gl.appendChild(sv('text', {x: 22, y: y + laneH / 2 + 14, class: 'lane-sub'}, sub));
      const hScale = laneH - 5;
      for (let b = 0; b < counts.length; b++) {
        const c = Number(counts[b]) || 0;
        if (!c || !mx) continue;
        const bh = Math.max(1.5, Math.sqrt(c / mx) * hScale), x = X0 + b * bw + 0.5, wd = Math.max(1, bw - 1.2);
        gl.appendChild(sv('rect', {x: x.toFixed(2), y: (base - bh).toFixed(2), width: wd.toFixed(2), height: bh.toFixed(2),
                                   class: cam ? 'bar cam' : 'bar'}));
        const al = cam ? 0 : Number(alerts[b]) || 0;
        if (al > 0) {
          const ah = Math.max(1.5, Math.sqrt(al / mx) * hScale);
          gl.appendChild(sv('rect', {x: x.toFixed(2), y: (base - ah).toFixed(2), width: wd.toFixed(2), height: ah.toFixed(2), class: 'bar alert'}));
        }
      }
      if (!cam && laneY[l.sensor] === undefined) laneY[l.sensor] = {y, h: laneH};
      if (cam) {
        for (const m of l.marks || []) {
          if (!inWin(m.ts)) continue;
          const px = xOf(m.ts);
          const tick = sv('line', {x1: px, x2: px, y1: y + 1, y2: y + 9, class: 'mark cam'});
          svTitle(tick, M(`cam #${m.id} · ${C.fmtTs(m.ts)} · ${m.reason || ''}`));
          gl.appendChild(tick);
        }
      }
      y += laneH + gap;
    });
    svg.appendChild(gl);
    if (shown.length < all.length) {
      svg.appendChild(sv('text', {x: X1, y: lanesBottom + 20, class: 'tl-more', 'text-anchor': 'end'},
                         `+${all.length - shown.length} MORE LANES`));
    }

    // high+ event marks as ticks on their lane
    const gm = sv('g', {class: 'tl-marks'});
    const hl = V.hl && V.hl.cite === 'sec' ? V.hl.id : null;
    for (const m of tl.marks || []) {
      const ly = laneY[m.sensor];
      if (!ly || !inWin(m.ts)) continue;
      const a = C.appear(ctx.first, 'm:' + m.id, ctx.t);
      if (a <= 0) continue;
      const px = xOf(m.ts), crit = m.sev === 'critical';
      const tick = sv('line', {x1: px, x2: px, y1: ly.y + 1, y2: ly.y + (crit ? 12 : 8), class: 'mark' + (crit ? ' crit' : ''),
                               opacity: a < 1 ? a.toFixed(3) : null});
      svTitle(tick, M(`#${m.id} · ${m.t || C.fmtTs(m.ts)} · ${m.sig || m.msg || m.kind || ''}`));
      gm.appendChild(tick);
      if (hl !== null && m.id === hl) {
        gm.appendChild(sv('line', {x1: px, x2: px, y1: spanTop - 6, y2: lanesBottom + 4, class: 'mark-hl'}));
        gm.appendChild(sv('text', {x: px + 6, y: spanTop + 2, class: 'mark-hl-label'}, `#${m.id}`));
      }
    }
    svg.appendChild(gm);

    // cursor at the newest event time
    if (hasCursor) {
      const px = Math.min(X1, Math.max(X0, xOf(ctx.clock)));
      const gc = sv('g', {class: 'tl-cursor'});
      gc.appendChild(sv('line', {x1: px, x2: px, y1: lanesTop - 8, y2: lanesBottom + 4, class: 'cursor'}));
      const label = C.fmtClock(ctx.clock, !tl.year_assumed).slice(-8);
      gc.appendChild(sv('rect', {x: tagX - tagW / 2, y: lanesBottom + 8, width: tagW, height: 17, rx: 2, class: 'cursor-tag'}));
      gc.appendChild(sv('text', {x: tagX, y: lanesBottom + 20.5, class: 'cursor-text', 'text-anchor': 'middle'}, label));
      svg.appendChild(gc);
    }
  }

  /* ---------------------------------------------------------------- entity graph */
  const COL_TITLE = ['SOURCES', 'SIGNATURES · USERS', 'TARGETS', 'CAMERAS'];
  function edgePath(a, b) {
    if (Math.abs(a.x - b.x) < 1) {                                  // same column: a bow to the right
      const bow = 46 + Math.min(60, Math.abs(a.y - b.y) * 0.25);
      return `M${a.x + a.r},${a.y} C${a.x + bow},${a.y} ${b.x + bow},${b.y} ${b.x + b.r},${b.y}`;
    }
    const dir = b.x > a.x ? 1 : -1, x1 = a.x + dir * a.r, x2 = b.x - dir * b.r, mid = (x2 - x1) / 2;
    return `M${x1.toFixed(1)},${a.y} C${(x1 + mid).toFixed(1)},${a.y} ${(x2 - mid).toFixed(1)},${b.y} ${x2.toFixed(1)},${b.y}`;
  }
  function renderGraph(st, ctx) {
    const svg = $('#gr-svg');
    svg.replaceChildren();
    const g = st.graph, nodes = (g && g.nodes) || [], edges = (g && g.edges) || [];
    $('#gr-empty').hidden = !g || nodes.length > 0;
    let count = '';
    if (g && g.truncated && g.totals) {
      count = `${C.fmtInt(nodes.length)} OF ${C.fmtInt(g.totals.nodes)} ENTITIES · ${C.fmtInt(edges.length)} OF ` +
              `${C.fmtInt(g.totals.edges)} LINKS · STRONGEST SHOWN`;
    } else if (g) {
      count = `${C.fmtInt(nodes.length)} ENTITIES · ${C.fmtInt(edges.length)} LINKS`;
    }
    $('#gr-count').textContent = count;
    const {w: W, h: H} = sizeOf(svg);
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    if (!nodes.length) return;
    const pos = C.layoutGraph(g, W, H);

    // column titles
    const cols = {};
    for (const id in pos) if (cols[pos[id].col] === undefined) cols[pos[id].col] = pos[id];
    const gt = sv('g', {class: 'gr-cols'});
    const order = Object.keys(cols).map(Number).sort((a, b) => a - b);
    order.forEach((ci, k) => {
      const p = cols[ci], last = k === order.length - 1 && order.length > 1;
      const anchor = p.side === 'l' ? 'end' : last || order.length === 1 ? 'start' : 'middle';
      gt.appendChild(sv('text', {x: p.x, y: 16, class: 'gr-col', 'text-anchor': anchor,
                                 dx: anchor === 'end' ? 6 : anchor === 'start' ? -6 : 0}, COL_TITLE[ci]));
    });
    svg.appendChild(gt);

    const hl = V.hl;
    const lit = e => hl && ((hl.cite === 'sec' && (e.evidence || []).includes(hl.id)) ||
                            (hl.cite === 'cam' && (e.camera_evidence || []).includes(hl.id)));
    const litNodes = new Set();
    if (hl) for (const e of edges) if (lit(e)) { litNodes.add(e.src); litNodes.add(e.dst); }

    const ge = sv('g', {class: 'gr-edges'});
    const sorted = edges.slice().sort((a, b) => C.sevRank(a.severity) - C.sevRank(b.severity) || (a.count || 0) - (b.count || 0));
    for (const e of sorted) {
      const a = pos[e.src], b = pos[e.dst];
      if (!a || !b) continue;
      const vis = C.appear(ctx.first, C.edgeKey(e), ctx.t);
      if (vis <= 0) continue;
      const sev = C.sevRank(e.severity);
      let cls = 'edge' + (e.kind === 'site_map' ? ' site' : sev >= 3 ? ' hi' : sev === 2 ? ' med' : '');
      if (hl) cls += lit(e) ? ' lit' : ' dim';
      const width = Math.min(6, 0.8 + 1.1 * Math.log10(1 + (Number(e.count) || 0))) + (hl && lit(e) ? 1 : 0);
      const path = sv('path', {d: edgePath(a, b), class: cls, 'stroke-width': width.toFixed(2), opacity: vis < 1 ? vis.toFixed(3) : null});
      svTitle(path, M(`${e.kind} · ${C.fmtInt(e.count || 0)} event${e.count === 1 ? '' : 's'} · ${e.src} → ${e.dst}`));
      ge.appendChild(path);
    }
    svg.appendChild(ge);

    const gn = sv('g', {class: 'gr-nodes'}), gl = sv('g', {class: 'gr-labels'});
    const colSize = {};
    for (const id in pos) colSize[pos[id].col] = (colSize[pos[id].col] || 0) + 1;
    for (const n of nodes) {
      const p = pos[n.id];
      if (!p) continue;
      const vis = C.appear(ctx.first, 'n:' + n.id, ctx.t);
      if (vis <= 0) continue;
      const pr = n.props || {};
      let cls = 'node ' + n.type;
      if (n.type === 'ip' || n.type === 'host') {
        if (pr.threat) cls += ' threat';
        if (pr.internal) cls += ' internal';
        if (pr.sensor) cls += ' sensor';
      }
      if (n.type === 'sig') cls += ' sev-' + (pr.severity || (pr.rule ? 'medium' : 'info'));
      if (hl) cls += litNodes.has(n.id) ? ' lit' : ' dim';
      const grp = sv('g', {class: cls, opacity: vis < 1 ? vis.toFixed(3) : null});
      const r = p.r * (0.7 + 0.3 * vis);
      if (n.type === 'camera') grp.appendChild(sv('rect', {x: p.x - r, y: p.y - r, width: 2 * r, height: 2 * r, rx: 1.5}));
      else if (n.type === 'sig') {
        const d = r * 1.25;
        grp.appendChild(sv('path', {d: `M${p.x},${p.y - d} L${p.x + d},${p.y} L${p.x},${p.y + d} L${p.x - d},${p.y} Z`}));
      } else {
        if (pr.sensor) grp.appendChild(sv('circle', {cx: p.x, cy: p.y, r: r + 3.5, class: 'ring'}));
        grp.appendChild(sv('circle', {cx: p.x, cy: p.y, r}));
      }
      const tip = [n.label || n.id];
      if (n.type === 'ip' || n.type === 'host') {
        tip.push(`${C.fmtInt(pr.events || 0)} events · ${pr.internal ? 'internal' : 'external'} · ${pr.role || ''}`);
        if ((pr.sensors || []).length) tip.push('sensors ' + pr.sensors.join(', '));
        if ((pr.rules || []).length) tip.push('rules ' + pr.rules.join(', '));
      } else if (n.type === 'user') tip.push(`${C.fmtInt(pr.attempts || 0)} attempts · ${C.fmtInt(pr.sources || 0)} sources`);
      else if (n.type === 'sig') tip.push(`${C.fmtInt(pr.count || 0)} events · ${pr.severity || pr.rule || ''}`);
      else if (n.type === 'camera') tip.push(`${C.fmtInt(pr.events || 0)} camera events`);
      svTitle(grp, M(tip.join('\n')));
      gn.appendChild(grp);

      if (!p.label && !pr.threat && !litNodes.has(n.id)) continue;
      const left = p.side === 'l', lx = left ? p.x - r - 9 : p.x + r + 9;
      const chars = Math.min(28, Math.floor((p.room === undefined ? 200 : p.room) / 6.9));
      if (chars < 4) continue;
      const lab = sv('text', {x: lx, y: p.y + 4, class: 'glab ' + n.type + (pr.threat ? ' threat' : '') + (hl ? (litNodes.has(n.id) ? ' lit' : ' dim') : ''),
                              'text-anchor': left ? 'end' : 'start', opacity: vis < 1 ? vis.toFixed(3) : null},
                    C.trunc(M(n.label || n.id), chars));
      gl.appendChild(lab);
      const vroom = (H - 56) / (colSize[p.col] || 1);
      if (vroom >= 34 && (n.type === 'ip' || n.type === 'host' || n.type === 'camera')) {
        const sub = [C.fmtInt(pr.events || 0) + ' ev'];
        if ((pr.sensors || []).length > 1) sub.push(pr.sensors.length + ' sensors');
        else if (pr.internal) sub.push('internal');
        gl.appendChild(sv('text', {x: lx, y: p.y + 17, class: 'glab-sub', 'text-anchor': left ? 'end' : 'start',
                                   opacity: vis < 1 ? vis.toFixed(3) : null}, sub.join(' · ')));
      }
    }
    svg.appendChild(gn);
    svg.appendChild(gl);
  }

  /* ---------------------------------------------------------------- incident */
  function citeMap(inc) {
    const m = new Map();
    for (const c of (inc && inc.citations) || []) m.set(c.kind + ':' + c.id, c);
    return m;
  }
  function chip(part, cites, interactive) {
    const c = cites.get(part.cite + ':' + part.id), found = !!(c && c.found);
    const node = el('span', 'cite ' + part.cite + (found ? '' : ' off'), part.label);
    if (V.hl && V.hl.cite === part.cite && V.hl.id === part.id) node.classList.add('on');
    if (found && interactive) {
      node.tabIndex = 0;
      node.setAttribute('role', 'button');
      node.addEventListener('click', ev => { ev.stopPropagation(); toggleHighlight(part.cite, part.id); });
      node.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleHighlight(part.cite, part.id); }
      });
      node.addEventListener('mouseenter', ev => showTip(ev, citeTip(part, c)));
      node.addEventListener('mouseleave', hideTip);
    }
    return node;
  }
  function citeTip(part, c) {
    const e = (c && c.event) || {};
    if (part.cite === 'cam') {
      return [`cam #${part.id} · ${e.camera || ''}`, C.fmtTs(e.ts), e.reason || '', e.answer || ''].filter(Boolean);
    }
    const head = [`#${part.id}`, e.sensor, e.kind, e.sev ? String(e.sev).toUpperCase() : ''].filter(Boolean).join(' · ');
    const route = e.src || e.dst ? `${e.src || '?'} → ${e.dst || '?'}` : '';
    return [head, e.t || '', route, e.user ? 'user ' + e.user : '', e.sig || '', e.msg || ''].filter(Boolean);
  }
  // One summary line: text, **bold** runs and citation chips. A leading "- " becomes a list line.
  function richLine(line, cites, interactive) {
    let s = line, li = false;
    const m = /^\s*(?:[-*•]|\d+[.)])\s+/.exec(s);
    if (m) { li = true; s = s.slice(m[0].length); }
    const h = /^\s*#{1,4}\s+/.exec(s);
    if (h) s = s.slice(h[0].length);
    const p = el('p', li ? 'li' : h ? 'hd' : null);
    s.split(/(\*\*[^*]+\*\*)/).forEach(seg => {
      if (!seg) return;
      const bold = /^\*\*[^*]+\*\*$/.test(seg), target = bold ? p.appendChild(el('strong')) : p;
      for (const part of C.splitCitations(bold ? seg.slice(2, -2) : seg)) {
        if (part.cite) target.appendChild(chip(part, cites, interactive));
        else target.appendChild(document.createTextNode(M(part.text)));
      }
    });
    return p;
  }
  function renderIncident(st, ctx) {
    const inc = st.incident, run = inc && inc.run;
    const wrap = $('#inc-wrap'), empty = $('#inc-empty'), status = $('#inc-status');
    wrap.hidden = !run;
    empty.hidden = !!run || !inc;
    $('#inc-run').textContent = '';
    status.hidden = !run;
    if (!run) return;
    const a = C.appear(ctx.first, 'r:' + run.id, ctx.t);
    fadeStyle(wrap, a, 8);
    if (a >= 1) { wrap.style.opacity = ''; wrap.style.transform = ''; }
    status.textContent = run.active ? 'RUNNING' : String(run.status || '').toUpperCase();
    status.className = 'status ' + (run.active ? 'run' : 'idle');
    const meta = [];
    if (run.mode) meta.push(String(run.mode).replace(/_/g, ' ').toUpperCase());
    if (C.num(run.created) !== null && C.num(run.ended) !== null) meta.push(C.fmtDur(run.ended - run.created).toUpperCase());
    $('#inc-run').textContent = meta.join(' · ');
    $('#inc-title').textContent = M(run.title || '');
    const cites = citeMap(inc), interactive = V.mode === 'live';
    const nSec = [...cites.values()].filter(c => c.kind === 'sec' && c.found).length;
    const nCam = [...cites.values()].filter(c => c.kind === 'cam' && c.found).length;
    const m2 = [`RUN ${run.id || ''}`];
    if (nSec) m2.push(`${nSec} CITED EVENT${nSec === 1 ? '' : 'S'}`);
    if (nCam) m2.push(`${nCam} CAMERA EVENT${nCam === 1 ? '' : 'S'}`);
    if ((inc.actions || []).length) m2.push(`${inc.actions.length} CONTAINMENT`);
    $('#inc-meta').textContent = M(m2.join(' · '));

    const body = $('#inc-body');
    body.replaceChildren();
    const summary = String(inc.summary || '').trim();
    if (summary) {
      const sa = C.appear(ctx.first, `r:${run.id}:summary`, ctx.t);
      for (const line of summary.split(/\n+/)) if (line.trim()) body.appendChild(richLine(line, cites, interactive));
      fadeStyle(body, sa, 6);
      if (sa >= 1) { body.style.opacity = ''; body.style.transform = ''; }
    } else {
      body.style.opacity = ''; body.style.transform = '';
      body.appendChild(el('p', 'quiet', run.active ? 'The desk is working on it.' : 'The run ended without a summary.'));
    }

    const act = $('#inc-activity');
    act.replaceChildren();
    if (run.active) {
      for (const x of (inc.activity || []).slice(-6)) {
        const row = el('div', 'act');
        row.appendChild(el('span', 'who', M(x.agent || '')));
        row.appendChild(el('span', 'sep', ' · '));
        row.appendChild(el('span', 'what', M(x.text || '')));
        act.appendChild(fadeStyle(row, C.appear(ctx.first, C.actKey(run.id, x), ctx.t), 4));
      }
    }
    $('#inc-verified').textContent = M(inc.verified || '');
  }

  /* ---------------------------------------------------------------- right rail: containment, detections, notices */
  function pressedAt(ctx, id) {
    for (let i = ctx.ui.length - 1; i >= 0; i--) {
      const u = ctx.ui[i];
      if (u.id === id && (u.action === 'approve' || u.action === 'reject') && ctx.t - u.t < C.PRESS_S) return u.action;
    }
    return null;
  }
  function renderCard(a, ctx) {
    const node = $('#tpl-card').content.firstElementChild.cloneNode(true);
    const pending = a.status === 'pending';
    node.classList.add(pending ? 'pending' : 'st-' + String(a.status || '').replace(/[^a-z_]/g, ''));
    node.querySelector('.state').textContent = C.cardHeader(a.status);
    node.querySelector('.card-id').textContent = '#' + a.id;
    node.querySelector('.card-q').textContent = M(a.question || '');
    const why = String(a.justification || '').trim();
    node.querySelector('.card-why').textContent = M(why);
    node.querySelector('.card-why').hidden = !why;

    const ev = node.querySelector('.card-ev');
    const evids = (a.evidence || []).map(Number);
    const missing = new Set();
    for (const v of (a.policy && a.policy.violations) || []) {
      const m = /^evidence #(\d+) is not on this desk$/.exec(v);
      if (m) missing.add(Number(m[1]));
    }
    const cap = ctx.cards >= 3 ? 4 : 12;                    // three cards share the rail: one row of chips, then +N
    for (const id of evids.slice(0, cap)) ev.appendChild(el('span', 'cite sec' + (missing.has(id) ? ' off' : ''), `[#${id}]`));
    if (evids.length > cap) ev.appendChild(el('span', 'more', `+${evids.length - cap}`));
    ev.hidden = !evids.length;

    const pol = node.querySelector('.card-policy');
    const pv = a.policy || {};
    if (pv.ok) {
      const ok = el('p', 'ok');
      ok.appendChild(el('i', 'tick'));
      ok.appendChild(document.createTextNode(pv.line || C.POLICY_LINE));
      pol.appendChild(ok);
    } else {
      for (const v of pv.violations || []) pol.appendChild(el('p', 'bad', M(v)));
    }
    pol.hidden = !pol.childNodes.length;
    const conn = C.connectorLine(a);
    node.querySelector('.card-conn').textContent = M(conn);
    node.querySelector('.card-conn').hidden = !conn;

    const btns = node.querySelector('.card-btns');
    const ap = node.querySelector('.approve'), rj = node.querySelector('.reject');
    ap.textContent = 'Approve';
    rj.textContent = 'Reject';
    if (pending) {
      const press = V.mode === 'live' ? V.busy[a.id] : pressedAt(ctx, a.id);
      if (press === 'approve') ap.classList.add('pressed');
      if (press === 'reject') rj.classList.add('pressed');
      if (V.mode === 'live') {
        ap.disabled = rj.disabled = !!V.busy[a.id];
        ap.addEventListener('click', () => decide(a.id, 'approved'));
        rj.addEventListener('click', () => decide(a.id, 'rejected'));
      } else {
        ap.tabIndex = rj.tabIndex = -1;
      }
    } else {
      btns.remove();
    }

    const log = node.querySelector('.card-log'), res = node.querySelector('.card-result');
    const line = C.decisionLine(a, V.tz);
    log.textContent = M(line);
    log.hidden = !line;
    res.textContent = M(a.note || '');
    res.hidden = pending || !a.note;
    if (a.status === 'failed') res.classList.add('bad');
    if (!pending) {
      const da = C.appear(ctx.first, `c:${a.id}:${a.status}`, ctx.t);
      fadeStyle(log, da, 4);
      fadeStyle(res, da, 4);
    }
    return fadeStyle(node, C.appear(ctx.first, 'c:' + a.id, ctx.t), 10);
  }
  function renderRail(st, ctx) {
    const list = C.actionsOf(st.containment);
    const cards = $('#cards');
    cards.replaceChildren();
    const shown = C.orderCards(list);
    cards.dataset.n = String(shown.length);              // 2-3 cards: decided ones go compact so the rail still fits
    for (const a of shown) cards.appendChild(renderCard(a, Object.assign({}, ctx, {cards: shown.length})));
    if (!shown.length && st.containment) {
      const none = $('#tpl-none').content.firstElementChild.cloneNode(true);
      none.querySelector('.micro').textContent = 'CONTAINMENT · NONE PROPOSED';
      cards.appendChild(none);
    }

    const dets = st.detections, rows = (dets && dets.detections) || [];
    const ol = $('#d-list');
    ol.replaceChildren();
    for (const d of rows.slice(0, 8)) {
      const li = $('#tpl-det').content.firstElementChild.cloneNode(true);
      const sev = li.querySelector('.sev');
      sev.textContent = C.SEV_SHORT[d.severity] || String(d.severity || '').toUpperCase();
      sev.classList.add(C.SEV_RANK[d.severity] !== undefined ? d.severity : 'info');   // class names never come raw from data
      li.querySelector('.det-title').textContent = M(d.title || d.id);
      const yr = !(d.details && d.details.year_assumed);
      const meta = [(d.sensors || []).join(' · '), C.fmtRange(d.first_ts, d.last_ts, yr),
                    `${C.fmtInt(d.evidence_total || 0)} ev`];
      li.querySelector('.det-meta').textContent = M(meta.filter(Boolean).join('  ·  '));
      if (d.run_id) li.classList.add('has-run');
      ol.appendChild(fadeStyle(li, C.appear(ctx.first, 'd:' + d.id, ctx.t), 6));
    }
    // Only whole rows: drop the ones the panel cannot show in full (layout offsets, so a row mid-fade still counts).
    while (ol.lastElementChild && ol.lastElementChild.offsetTop - ol.offsetTop + ol.lastElementChild.offsetHeight > ol.clientHeight) {
      ol.removeChild(ol.lastElementChild);
    }
    $('#d-empty').hidden = !dets || rows.length > 0;
    const bs = (dets && dets.by_severity) || {};
    const sum = ['critical', 'high', 'medium', 'low'].filter(k => bs[k]).map(k => `${C.fmtInt(bs[k])} ${C.SEV_SHORT[k]}`);
    $('#d-sum').textContent = dets ? (sum.length ? sum.join(' · ') : '') : '';
    const trig = dets && dets.trigger_status;
    $('#d-trigger').textContent = trig && trig.text ? M('No run started: ' + trig.text) : '';
    $('#d-trigger').hidden = !(trig && trig.text);

    const cfg = st.config || {}, notices = cfg.notices || {};
    $('#f-nvd').textContent = notices.nvd || C.NVD_NOTICE;
    $('#f-dbip').textContent = notices.dbip || '';
    $('#f-dbip').hidden = !notices.dbip;
    const other = st.containment && !Array.isArray(st.containment) ? Number(st.containment.other_pending) || 0 : 0;
    $('#f-other').textContent = other > 0 ? `${C.fmtInt(other)} other approval${other === 1 ? '' : 's'} waiting` : '';
    $('#f-other').hidden = !(other > 0);
    const credit = V.mode === 'replay' ? (V.bundle && V.bundle.query && V.bundle.query.credit) || Q.get('credit') : Q.get('credit');
    $('#f-credit').textContent = credit === 'secrepo' ? C.SECREPO_CREDIT : '';
    $('#f-credit').hidden = credit !== 'secrepo';
  }

  /* ---------------------------------------------------------------- LIVE interaction: tooltip, highlight, decisions */
  function showTip(ev, lines) {
    const tip = $('#tip');
    tip.replaceChildren(...lines.map((l, i) => el('div', i ? 'tl' : 'th', M(l))));
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - r.width - 12, ev.clientX + 14) + 'px';
    tip.style.top = Math.min(window.innerHeight - r.height - 12, ev.clientY + 16) + 'px';
  }
  function hideTip() { $('#tip').hidden = true; }
  function toggleHighlight(cite, id) {
    V.hl = V.hl && V.hl.cite === cite && V.hl.id === id ? null : {cite, id};
    renderNow();
  }
  async function decide(id, status) {
    if (V.busy[id]) return;
    V.busy[id] = status === 'approved' ? 'approve' : 'reject';
    if (V.rec) V.rec.ui(V.busy[id], id);
    renderNow();
    try {
      const r = await fetch(`/api/actions/${id}/decide`, {method: 'POST', credentials: 'same-origin',
                                                           headers: {'Content-Type': 'application/json'},
                                                           body: JSON.stringify({status})});
      if (r.status === 401) return toLogin();
      if (!r.ok) flash(`Decision not saved (HTTP ${r.status})`);
    } catch (_) {
      flash('Decision not saved: the desk did not answer');
    }
    delete V.busy[id];
    await Promise.all([fetchKind('containment'), fetchKind('incident')]);
    renderNow();
  }
  function flash(text) {
    const n = $('#flash');
    n.textContent = text;
    n.hidden = false;
    clearTimeout(flash.t);
    flash.t = setTimeout(() => { n.hidden = true; }, 4000);
  }
  function toLogin() {
    V.stopped = true;
    location.href = '/login?next=/desk/cyber';
  }
  function overlay(text) {
    $('#ov-text').textContent = text || '';
    $('#overlay').hidden = !text;
  }

  /* ---------------------------------------------------------------- LIVE polling */
  function routeUrl(kind) {
    const qs = new URLSearchParams();
    if (WINDOWED.includes(kind)) for (const k of ['since', 'until']) if (Q.get(k)) qs.set(k, Q.get(k));
    const s = qs.toString();
    return ROUTE[kind] + (s ? '?' + s : '');
  }
  async function fetchKind(kind) {
    if (V.stopped) return;
    try {
      const r = await fetch(routeUrl(kind), {credentials: 'same-origin', cache: 'no-store'});
      if (r.status === 401) return toLogin();
      if (r.status === 409) { overlay('No desk selected'); return; }
      if (!r.ok) return;
      const text = await r.text();
      if (V.stopped) return;
      overlay('');
      V.seen.add(kind);
      if (text === V.raw[kind]) return;
      V.raw[kind] = text;
      const data = JSON.parse(text);
      V.data[kind] = data;
      const now = pageClock(), initial = !V.firstRender;
      for (const k of C.appearKeys(kind, data)) {
        if (!V.first.has(k)) {
          V.first.set(k, initial ? -1 : now);
          if (!initial) V.animUntil = Math.max(V.animUntil, now + C.ANIM_S + 0.05);
        }
      }
      if (V.rec) V.rec.frame(kind, data);
      scheduleRender();
    } catch (_) { /* the desk did not answer: keep the last good state on screen */ }
  }
  function pollLoop(kind) {
    if (V.stopped) return;
    fetchKind(kind).finally(() => { if (!V.stopped) setTimeout(() => pollLoop(kind), POLL_MS[kind]); });
  }
  function scheduleRender() {
    if (V.rafQueued) return;
    V.rafQueued = true;
    requestAnimationFrame(() => {
      V.rafQueued = false;
      renderNow();
      if (pageClock() < V.animUntil) scheduleRender();
    });
  }
  function renderNow() {
    if (V.mode !== 'live') return;
    renderAll(liveState(), pageClock());
    if (!V.firstRender && V.seen.size >= Object.keys(ROUTE).length) {
      V.firstRender = true;
      fontsReady().then(() => readyResolve(true));
    }
  }
  function fontsReady() {
    if (!document.fonts) return Promise.resolve();
    return Promise.all(FACES.map(f => document.fonts.load(f).catch(() => null))).then(() => document.fonts.ready);
  }
  async function bootLive() {
    V.tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || 'UTC';
    setMode();
    const rec = Q.get('record');
    if (rec !== null) startRecorder(rec);
    for (const kind of Object.keys(ROUTE)) pollLoop(kind);
    try {
      const r = await fetch('/api/config', {credentials: 'same-origin', cache: 'no-store'});
      if (r.status === 401) return toLogin();
      const c = r.ok ? await r.json() : {};
      V.desk = {id: c.desk ? c.desk.id : null, name: (c.business && c.business.name) || (c.desk && c.desk.name) || ''};
      if (c.needs_desk) overlay('No desk selected');
      scheduleRender();
    } catch (_) { /* name stays empty */ }
  }

  /* ---------------------------------------------------------------- record mode */
  function startRecorder(raw) {
    const name = String(raw || '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+/, '').slice(0, 64) || 'recording';
    const rec = V.rec = {name, t0: performance.now(), frames: [], last: {}, held: {}, saved: 0, dirty: false};
    const stamp = () => Math.round((performance.now() - rec.t0)) / 1000;
    rec.frame = (kind, data) => {
      // CONTRACT-DEVIATION: the config frame's hook_url carries the desk's hook token (a credential that lets anyone
      // post log lines). Recordings travel for capture, so the token is replaced; the page never shows hook_url.
      if (kind === 'config' && data && typeof data.hook_url === 'string') {
        data = Object.assign({}, data, {hook_url: data.hook_url.replace(/\/hook\/[^/?#]+/, '/hook/<token>')});
      }
      // CONTRACT-DEVIATION (spec 8.6 "every response that differs"): a timeline answer differs on every poll only by
      // `now` (the server clock; the page never shows it). Keep a frame per real change plus the poll just before it,
      // so REPLAY draws the same and its clock still moves only between two real observations, while a quiet stretch
      // (waiting for an approval) adds no megabytes to a recording capped at 20 MB by default.
      const s = JSON.stringify(kind === 'timeline' && data && typeof data === 'object' ? Object.assign({}, data, {now: 0}) : data);
      if (rec.last[kind] === s) { rec.held[kind] = {t: stamp(), kind, data}; return; }
      rec.last[kind] = s;
      const held = rec.held[kind];
      if (held) {                                         // in time order: other kinds may have answered since
        let i = rec.frames.length;
        while (i > 0 && rec.frames[i - 1].t > held.t) i--;
        rec.frames.splice(i, 0, held);
      }
      delete rec.held[kind];
      rec.frames.push({t: stamp(), kind, data});
      rec.dirty = true;
      recState();
    };
    rec.ui = (action, id) => { rec.frames.push({t: stamp(), kind: 'ui', data: {action, id}}); rec.dirty = true; recState(); };
    rec.bundle = () => ({
      kind: 'atlas-cyber-recording', version: 1, name: rec.name, created: Date.now() / 1000,
      desk: {id: V.desk.id, name: V.desk.name}, tz: V.tz, viewport: [window.innerWidth, window.innerHeight],
      mask_public_ips: V.mask,
      query: Object.fromEntries(['since', 'until', 'credit'].filter(k => Q.get(k)).map(k => [k, Q.get(k)])),
      duration: stamp(), frames: rec.frames,
    });
    rec.save = async () => {
      const bundle = rec.bundle(), body = JSON.stringify({name: rec.name, bundle});
      rec.dirty = false;
      recState('saving…');
      try {
        const r = await fetch('/api/cyber/recordings', {method: 'POST', credentials: 'same-origin',
                                                       headers: {'Content-Type': 'application/json'}, body});
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { rec.dirty = true; recState(`not saved: ${j.error || 'HTTP ' + r.status}`); return j; }
        rec.saved = bundle.frames.length;
        recState(`saved ${C.clockIn(Date.now() / 1000, V.tz)} · ${(body.length / 1048576).toFixed(1)} MB`);
        return j;
      } catch (e) {
        rec.dirty = true;
        recState('not saved: the desk did not answer');
        return {error: String(e)};
      }
    };
    $('#rec-wrap').hidden = false;
    $('#rec-name').textContent = rec.name;
    $('#rec-save').addEventListener('click', () => rec.save());
    setInterval(() => { if (rec.dirty) rec.save(); }, 15000);
    window.__saveRecording = () => rec.save();
    recState();
  }
  function recState(text) {
    const rec = V.rec;
    if (!rec) return;
    $('#rec-state').textContent = text || `${C.fmtInt(rec.frames.length)} frames`;
  }

  /* ---------------------------------------------------------------- REPLAY mode */
  function setMode() {
    document.body.classList.toggle('replay', V.mode === 'replay');
    document.body.classList.toggle('live', V.mode === 'live');
    $('#mode-text').textContent = V.mode === 'replay' ? 'REPLAY' : 'LIVE';
    $('#mode').className = 'badge ' + V.mode;
  }
  async function loadBundle(b) {
    if (!b || b.kind !== 'atlas-cyber-recording') throw new Error('not an Atlas cyber recording');
    V.stopped = true;                                       // REPLAY never polls
    V.mode = 'replay';
    V.hl = null;
    V.bundle = b;
    V.tz = b.tz || 'UTC';
    V.desk = {id: b.desk ? b.desk.id : null, name: (b.desk && b.desk.name) || ''};
    const qm = Q.get('mask');
    V.mask = qm === '1' ? true : qm === '0' ? false : !!b.mask_public_ips;
    $('#rec-wrap').hidden = true;
    setMode();
    C.prepare(b);
    window.__duration = Number(b.duration) || 0;
    await fontsReady();
    window.__render(0);
    readyResolve(true);
    return true;
  }
  window.__render = function (t) {
    if (!V.bundle) return false;
    t = Number(t) || 0;
    V.lastT = t;
    const st = C.stateAt(V.bundle, t);
    renderAll(st, t);
    return true;
  };
  // ?play=1 previews a recording in real time for a person. Captures never use it: they call __render(t) per frame.
  function preview() {
    const t0 = performance.now(), dur = Number(V.bundle.duration) || 0;
    const step = () => {
      const t = Math.min(dur, (performance.now() - t0) / 1000);
      window.__render(t);
      if (t < dur) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  window.__loadBundle = obj => loadBundle(obj);
  async function bootReplay(name) {
    setMode();
    if (!C.NAME_RE.test(name)) return;                     // e.g. "__test": wait for __loadBundle(obj)
    try {
      const r = await fetch('/api/cyber/recordings/' + encodeURIComponent(name), {credentials: 'same-origin'});
      if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent('/desk/cyber?replay=' + name); return; }
      if (r.status === 409) { overlay('No desk selected'); return; }
      if (!r.ok) { overlay(`Recording ${name} not found`); return; }
      await loadBundle(await r.json());
      if (Q.get('play') === '1') preview();
    } catch (e) {
      overlay(`Recording ${name} could not be loaded`);
    }
  }

  /* ---------------------------------------------------------------- boot */
  function boot() {
    document.addEventListener('click', () => { if (V.hl) { V.hl = null; renderNow(); } hideTip(); });
    window.addEventListener('resize', () => {
      if (V.mode === 'replay') { if (V.bundle) window.__render(V.lastT || 0); } else renderNow();
    });
    if (V.mode === 'replay') bootReplay(Q.get('replay') || '');
    else bootLive();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
