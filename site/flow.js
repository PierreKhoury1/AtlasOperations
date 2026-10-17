/* The workflow under the camera recording on the home page: what one run looks like after the cameras see something.
   Three specialists (vision, inbox, research), each on its own model session, report up to Atlas, the lead; Atlas
   writes the summary and a draft; the draft lands on the owner's phone (WhatsApp, Telegram or email, the visitor
   picks) as an approval; after the tap it goes out. A looping illustration of one run, drawn in the page, not a
   recording; the recording is the video above it. */
(function () {
  const root = document.getElementById("flow");
  if (!root) return;
  const $ = (s) => root.querySelector(s);
  const rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const AGENTS = {
    vision: { line: "dock 2 · 09:41 · lorry + 2 pallets, driver waiting 4 min", dur: 1300 },
    inbox: { line: "delivery note from Hauliers Ltd · PO 4471 · 2 pallets", dur: 1500 },
    research: { line: "PO 4471 matches order 18 Sep · receiving: Sam (on shift)", dur: 1700 },
  };
  const SUMMARY = "Delivery for PO 4471 is at dock 2 (09:41): two pallets, driver waiting, note matches the order. Nobody from receiving is on the floor. Drafted a message to Sam; waiting for your OK.";
  const DRAFT = "Hi Sam, the Hauliers delivery for PO 4471 is at dock 2, two pallets, driver waiting. Can you receive it? Atlas, Acme Warehouse desk";
  const CH = { whatsapp: { name: "WhatsApp", to: "Sam · Receiving", tick: "✓✓" }, telegram: { name: "Telegram", to: "Sam", tick: "✓✓" }, email: { name: "Email", to: "sam@acme.example", tick: "sent" } };
  let chan = "whatsapp";

  function reset() {
    root.querySelectorAll(".fa").forEach((a) => { a.classList.remove("on", "done"); a.querySelector(".out").textContent = ""; });
    root.querySelectorAll(".wire").forEach((w) => w.classList.remove("on"));
    $("#f-lead").classList.remove("on", "done");
    $("#f-sum").textContent = ""; $("#f-sum").classList.remove("cursor");
    $("#f-phone").className = "phone"; $("#f-draft").textContent = ""; $("#f-stamp").textContent = "";
    $("#f-state").textContent = "waiting for the cameras";
  }
  function type(el, text, ms) {
    return new Promise((res) => {
      if (rm) { el.textContent = text; res(); return; }
      let i = 0; el.textContent = ""; el.classList.add("cursor");
      const step = Math.max(1, Math.round(text.length / (ms / 30)));
      const t = setInterval(() => { i = Math.min(text.length, i + step); el.textContent = text.slice(0, i); if (i >= text.length) { clearInterval(t); el.classList.remove("cursor"); res(); } }, 30);
    });
  }
  const wait = (ms) => new Promise((r) => setTimeout(r, rm ? 0 : ms));

  async function run() {
    reset();
    $("#f-state").textContent = "run 4820 · started by a camera rule";
    for (const [k, a] of Object.entries(AGENTS)) {
      const el = $(`.fa[data-a="${k}"]`); el.classList.add("on");
      await type(el.querySelector(".out"), a.line, a.dur);
      el.classList.remove("on"); el.classList.add("done");
      $(`.wire[data-a="${k}"]`).classList.add("on");
      await wait(250);
    }
    $("#f-lead").classList.add("on"); $("#f-state").textContent = "Atlas · reading three reports";
    await wait(500);
    await type($("#f-sum"), SUMMARY, 3200);
    $("#f-lead").classList.remove("on"); $("#f-lead").classList.add("done");
    $("#f-state").textContent = "draft queued · waiting for your approval";
    const ph = $("#f-phone"); ph.classList.add("draft");
    $("#f-to").textContent = CH[chan].to; $("#f-ch").textContent = CH[chan].name;
    await type($("#f-draft"), DRAFT, 1800);
    await wait(900);
    ph.classList.add("press"); await wait(450); ph.classList.remove("press"); ph.classList.add("sent"); $("#f-sent").textContent = DRAFT;
    $("#f-stamp").textContent = `${CH[chan].tick} 09:42 · ${CH[chan].name} · approved by you`;
    $("#f-state").textContent = "sent · logged in the journal";
    await wait(5200);
  }
  root.querySelectorAll("[data-chan]").forEach((b) => b.addEventListener("click", () => {
    chan = b.dataset.chan; root.querySelectorAll("[data-chan]").forEach((x) => x.classList.toggle("on", x === b));
    $("#f-to").textContent = CH[chan].to; $("#f-ch").textContent = CH[chan].name;
  }));

  let visible = false, running = false;
  async function loop() { if (running) return; running = true; while (visible && !document.hidden) await run(); running = false; }
  new IntersectionObserver((es) => es.forEach((e) => { visible = e.isIntersecting; if (visible) loop(); }), { rootMargin: "80px" }).observe(root);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && visible) loop(); });
})();
