/* "Build your own team" on the home page: pick the business, the work that eats your time, channels, inputs, model
   and rules; the team forms on the right (one lead, the specialists the work needs, their tools and rules) and the
   brief that Atlas will receive is written out. "Build this desk" opens the workspace with that brief; nothing runs
   on the site itself. Everything here is a draft of what the desk would be, drawn from the same rules the desk uses. */
(function () {
  const root = document.getElementById("builder");
  if (!root) return;
  const $ = (s, el) => (el || root).querySelector(s);
  const $$ = (s, el) => Array.from((el || root).querySelectorAll(s));

  const BIZ = {
    estate: { name: "estate & lettings agency", work: ["enquiries", "arrears", "viewings", "repairs"], inputs: ["inbox", "crm", "records"], chan: ["email", "whatsapp"] },
    clinic: { name: "clinic", work: ["enquiries", "bookings"], inputs: ["inbox", "calendar", "documents"], chan: ["email", "whatsapp", "sms"] },
    ecom: { name: "online shop", work: ["enquiries", "content", "reports"], inputs: ["inbox", "documents", "crm"], chan: ["email", "instagram"] },
    consult: { name: "consultancy", work: ["proposals", "research", "reports"], inputs: ["inbox", "documents"], chan: ["email"] },
    warehouse: { name: "warehouse", work: ["cameras", "incidents"], inputs: ["cameras", "inbox"], chan: ["whatsapp", "telegram"] },
    trades: { name: "trades business", work: ["enquiries", "bookings", "leads"], inputs: ["inbox", "calendar", "crm"], chan: ["whatsapp", "sms", "email"] },
  };
  const WORK = {
    enquiries: "Enquiries that wait", proposals: "Proposals & quotes", arrears: "Rent arrears", viewings: "Viewings", repairs: "Repairs & maintenance",
    bookings: "Bookings & reminders", leads: "Finding leads", content: "Content & promos", research: "Research briefs", reports: "Reports",
    cameras: "Watching cameras", incidents: "Incidents & alerts",
  };
  const CHAN = { email: "Email", whatsapp: "WhatsApp", telegram: "Telegram", sms: "SMS", instagram: "Instagram" };
  const INPUTS = { inbox: "Inbox", cameras: "Cameras", calendar: "Calendar", crm: "CRM", records: "Records", documents: "Documents" };
  const MODELS = { claude: ["Claude session", "hosted by Atlas, yours while you work"], atlas: ["Atlas models", "free, nothing to set up"], own: ["Your own key", "Anthropic, OpenRouter, Gemini"] };
  const RULES = { approve: ["Nothing sent without your approval", true, true], prices: ["Never quote prices in writing", true, false], hours: ["Send only in office hours", false, false], plain: ["Plain English, no markdown", true, false], id: ["Never identify a person on camera", true, false] };

  // the specialist each kind of work calls for: name, goal, the inputs it uses, channels it writes on
  const SPEC = {
    enquiries: { n: "Enquiry triage", g: "reads every new message, drafts the reply, flags what needs you", uses: ["inbox"], writes: true },
    proposals: { n: "Proposal writer", g: "turns a brief into a client-ready proposal with pricing", uses: ["documents"], qa: true },
    arrears: { n: "Arrears chaser", g: "finds late rent, drafts firm polite reminders, skips agreed plans", uses: ["records"], writes: true },
    viewings: { n: "Viewings booker", g: "offers slots, confirms, reminds, reschedules", uses: ["calendar"], writes: true },
    repairs: { n: "Repairs desk", g: "logs the fault, asks the contractor, keeps the tenant told", uses: ["records"], writes: true },
    bookings: { n: "Booking agent", g: "books, reschedules and reminds; escalates what it should not decide", uses: ["calendar"], writes: true },
    leads: { n: "Lead researcher", g: "qualifies each lead from public sources before anyone replies", uses: ["crm"] },
    content: { n: "Content writer", g: "drafts posts, product copy and promos on a calendar", uses: ["documents"], qa: true },
    research: { n: "Researcher", g: "reads sources, keeps citations, writes the brief", uses: ["documents"] },
    reports: { n: "Reporter", g: "writes the daily and weekly report from the log, numbers first", uses: ["records"] },
    cameras: { n: "Vision agent", g: "watches the feeds through the detector, keeps a journal you can ask", uses: ["cameras"] },
    incidents: { n: "Incident agent", g: "turns a camera or inbox event into a case and the right message", uses: ["cameras", "inbox"], writes: true },
  };
  const ICON = {
    lead: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
    inbox: '<path d="M3.5 13.5 6 5h12l2.5 8.5V19a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1v-5.5Z"/><path d="M3.5 13.5H9l1 2h4l1-2h5.5"/>',
    cameras: '<rect x="3" y="6" width="13" height="12" rx="1.5"/><path d="m16 10.5 5-3v9l-5-3"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15" rx="1.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    crm: '<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><circle cx="17" cy="9" r="2.5"/><path d="M15.5 14.2A4.5 4.5 0 0 1 21 18.5"/>',
    records: '<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
    documents: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
    pen: '<path d="M4 20h4l10-10-4-4L4 16z"/><path d="M12.5 7.5l4 4"/>',
    qa: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  };
  const svg = (d) => `<svg viewBox="0 0 24 24">${d}</svg>`;

  // ---- state ------------------------------------------------------------------------------------------------
  const state = { biz: "estate", work: new Set(), chan: new Set(), inputs: new Set(), model: "claude", rules: new Set(["approve", "prices", "plain", "id"]) };
  function preset(k) {
    const b = BIZ[k]; state.biz = k; state.work = new Set(b.work); state.chan = new Set(b.chan); state.inputs = new Set(b.inputs);
    if (b.work.includes("cameras") || b.work.includes("incidents")) state.inputs.add("cameras");
  }
  preset("estate");

  // ---- the form ---------------------------------------------------------------------------------------------
  function chips(k, dict, multi) {
    return Object.entries(dict).map(([id, v]) => {
      const label = Array.isArray(v) ? v[0] : (typeof v === "object" ? v.name : v);
      const sub = Array.isArray(v) ? v[1] : "";
      return `<button type="button" class="chip" data-k="${k}" data-id="${id}" role="${multi ? "checkbox" : "radio"}">${label}${sub ? `<small>${sub}</small>` : ""}</button>`;
    }).join("");
  }
  $("#b-biz").innerHTML = chips("biz", BIZ, false);
  $("#b-work").innerHTML = chips("work", WORK, true);
  $("#b-chan").innerHTML = chips("chan", CHAN, true);
  $("#b-inputs").innerHTML = chips("inputs", INPUTS, true);
  $("#b-model").innerHTML = chips("model", MODELS, false);
  $("#b-rules").innerHTML = Object.entries(RULES).map(([id, [label, on, locked]]) =>
    `<label class="rule${locked ? " locked" : ""}"><input type="checkbox" data-rule="${id}" ${state.rules.has(id) ? "checked" : ""} ${locked ? "disabled" : ""}><span class="sw"></span><span>${label}${locked ? " <em>always on</em>" : ""}</span></label>`).join("");

  root.addEventListener("click", (e) => {
    const c = e.target.closest(".chip"); if (!c) return;
    const k = c.dataset.k, id = c.dataset.id;
    if (k === "biz") preset(id);
    else if (k === "model") state.model = id;
    else { const set = state[k]; set.has(id) ? set.delete(id) : set.add(id); if (k === "work" && (id === "cameras" || id === "incidents") && set.has(id)) state.inputs.add("cameras"); }
    render();
  });
  root.addEventListener("change", (e) => {
    const r = e.target.dataset && e.target.dataset.rule; if (!r) return;
    e.target.checked ? state.rules.add(r) : state.rules.delete(r); render();
  });

  // ---- the team ---------------------------------------------------------------------------------------------
  function team() {
    const specs = [], seen = new Set(); let qa = false;
    Array.from(state.work).forEach((w) => { const s = SPEC[w]; if (!s || seen.has(s.n)) return; seen.add(s.n); specs.push({ ...s, key: w }); if (s.qa) qa = true; });
    if (qa) specs.push({ n: "QA reviewer", g: "checks facts, tone and every rule before anything reaches you", uses: ["documents"], key: "qa" });
    return specs;
  }
  function modelBadge() { return MODELS[state.model][0]; }
  function card(s) {
    const inputs = s.uses.filter((u) => state.inputs.has(u));
    const chan = s.writes ? Array.from(state.chan).map((c) => CHAN[c]) : [];
    const tools = [...inputs.map((u) => INPUTS[u]), ...chan, ...(s.key === "qa" ? ["Policy"] : [])];
    const icon = s.key === "qa" ? ICON.qa : (s.writes ? ICON.pen : (ICON[s.uses[0]] || ICON.search));
    return `<div class="ag" data-key="${s.key}"><div class="ag-h">${svg(icon)}<b>${s.n}</b><span class="m">${modelBadge()}</span></div>
      <p>${s.g}</p><div class="tools">${tools.length ? tools.map((t) => `<span>${t}</span>`).join("") : '<span class="none">no inputs picked yet</span>'}</div>
      <div class="rules">${state.rules.size} rules · nothing sent without approval</div></div>`;
  }
  function briefText(specs) {
    const b = BIZ[state.biz];
    const work = Array.from(state.work).map((w) => WORK[w].toLowerCase()).join(", ") || "nothing picked yet";
    const chan = Array.from(state.chan).map((c) => CHAN[c]).join(", ") || "none";
    const inputs = Array.from(state.inputs).map((i) => INPUTS[i]).join(", ") || "none";
    const rules = Array.from(state.rules).map((r) => RULES[r][0].toLowerCase()).join("; ");
    return `We are a ${b.name}. The work that eats our time: ${work}. ` +
      `Give the desk these inputs: ${inputs}. It may write on: ${chan}. ` +
      `Team: Atlas leads; ${specs.length ? specs.map((s) => s.n.toLowerCase()).join(", ") : "pick the work and the specialists follow"}. ` +
      `Rules: ${rules}. Model: ${modelBadge()}.`;
  }
  function wires() {
    const stage = $("#b-stage"), svgEl = $("#b-wires"), lead = $("#b-lead"), ags = $$(".ag");
    if (!stage || !lead) return;
    const R = stage.getBoundingClientRect(), L = lead.getBoundingClientRect();
    svgEl.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`); svgEl.setAttribute("width", R.width); svgEl.setAttribute("height", R.height);
    const x0 = L.left - R.left + L.width / 2, y0 = L.bottom - R.top;
    svgEl.innerHTML = ags.map((a, i) => {
      const A = a.getBoundingClientRect(), x1 = A.left - R.left + A.width / 2, y1 = A.top - R.top;
      const my = (y0 + y1) / 2, d = `M${x0},${y0} C${x0},${my} ${x1},${my} ${x1},${y1}`;
      return `<path d="${d}" class="w"/><circle r="3.5" class="p"><animateMotion dur="${2.6 + (i % 3) * .5}s" begin="${i * .4}s" repeatCount="indefinite" path="${d}"/></circle>`;
    }).join("");
  }
  function render() {
    $$(".chip").forEach((c) => {
      const k = c.dataset.k, id = c.dataset.id;
      const on = k === "biz" ? state.biz === id : k === "model" ? state.model === id : state[k].has(id);
      c.classList.toggle("on", on); c.setAttribute("aria-checked", on ? "true" : "false");
    });
    const specs = team();
    $("#b-lead .m").textContent = modelBadge();
    $("#b-lead p").textContent = `briefs ${specs.length || "the"} specialist${specs.length === 1 ? "" : "s"}, checks their work, keeps the log; sends nothing itself`;
    $("#b-agents").innerHTML = specs.length ? specs.map(card).join("") : '<div class="ag empty">Pick the work that eats your time. The specialists appear here, one per job.</div>';
    $("#b-count").textContent = `${1 + specs.length} agent${specs.length ? "s" : ""} · ${state.rules.size} rules · ${Array.from(state.chan).length} channel${state.chan.size === 1 ? "" : "s"}`;
    const brief = briefText(specs);
    $("#b-brief").textContent = brief;
    $("#b-go").href = "/try?next=" + encodeURIComponent("/desk/workspace?new=1&brief=" + encodeURIComponent(brief));
    requestAnimationFrame(wires);
  }
  render();
  window.addEventListener("resize", () => requestAnimationFrame(wires));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => requestAnimationFrame(wires));
})();
