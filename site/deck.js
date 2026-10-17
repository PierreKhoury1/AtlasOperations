/* The home page as a deck: one screen per topic, side by side. The top bar's tabs, the ← → buttons, the arrow keys,
   a swipe or a horizontal drag move between screens; the hash (#build) names the screen so links still work. No page
   scroll; a screen taller than the window scrolls inside itself. */
(function () {
  const deck = document.getElementById("deck");
  if (!deck) return;
  const track = deck.querySelector(".track");
  const screens = Array.from(track.children);
  const tabs = Array.from(document.querySelectorAll("[data-screen]"));
  const prev = deck.querySelector(".arrow.prev"), next = deck.querySelector(".arrow.next");
  const dots = deck.querySelector(".deck-dots");
  const rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let i = 0;

  screens.forEach((s, k) => {
    const b = document.createElement("button"); b.type = "button"; b.setAttribute("aria-label", s.dataset.title || s.id);
    b.addEventListener("click", () => go(k)); dots.appendChild(b);
  });
  function go(k, push) {
    i = Math.max(0, Math.min(screens.length - 1, k));
    deck.scrollLeft = 0; track.scrollLeft = 0;                                   // the hash must never scroll the deck itself
    track.style.transform = `translateX(${-i * 100}%)`;
    screens.forEach((s, n) => { s.classList.toggle("on", n === i); s.setAttribute("aria-hidden", n === i ? "false" : "true"); s.toggleAttribute("inert", n !== i); });
    tabs.forEach((t) => t.classList.toggle("on", t.dataset.screen === key(screens[i])));
    Array.from(dots.children).forEach((d, n) => d.classList.toggle("on", n === i));
    prev.disabled = i === 0; next.disabled = i === screens.length - 1;
    const label = deck.querySelector(".arrow-label"); if (label) label.textContent = `${i + 1} / ${screens.length} · ${screens[i].dataset.title || ""}`;
    if (push !== false) history.replaceState(null, "", "#" + key(screens[i]));
    document.dispatchEvent(new CustomEvent("deck:screen", { detail: { id: key(screens[i]), index: i } }));
  }
  // screens are #s-<name>; links and the hash say #<name>, which matches no element, so the browser never scrolls the deck itself
  const key = (s) => s.id.replace(/^s-/, "");
  function byHash() { const id = (location.hash || "").slice(1); const k = screens.findIndex((s) => key(s) === id); go(k < 0 ? 0 : k, false); }
  tabs.forEach((t) => t.addEventListener("click", (e) => { const k = screens.findIndex((s) => key(s) === t.dataset.screen); if (k >= 0) { e.preventDefault(); go(k); } }));
  prev.addEventListener("click", () => go(i - 1)); next.addEventListener("click", () => go(i + 1));
  window.addEventListener("hashchange", byHash);
  window.addEventListener("keydown", (e) => {
    if (e.target && /^(input|textarea|select)$/i.test(e.target.tagName)) return;
    if (e.key === "ArrowRight") go(i + 1); else if (e.key === "ArrowLeft") go(i - 1);
  });
  // swipe / drag: horizontal intent only, so vertical scrolling inside a screen still works
  let sx = 0, sy = 0, down = false, moved = false;
  track.addEventListener("pointerdown", (e) => { if (e.button && e.button !== 0) return; down = true; moved = false; sx = e.clientX; sy = e.clientY; });
  track.addEventListener("pointermove", (e) => { if (!down) return; const dx = e.clientX - sx, dy = e.clientY - sy; if (!moved && Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) * 1.4) moved = true; });
  const end = (e) => { if (!down) return; down = false; const dx = e.clientX - sx; if (moved && Math.abs(dx) > 60) go(i + (dx < 0 ? 1 : -1)); };
  track.addEventListener("pointerup", end); track.addEventListener("pointercancel", () => { down = false; });
  track.addEventListener("click", (e) => { if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; } }, true);
  if (rm) track.style.transition = "none";
  deck.addEventListener("scroll", () => { if (deck.scrollLeft) deck.scrollLeft = 0; });
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  byHash(); setTimeout(() => { deck.scrollLeft = 0; }, 0);
  window.Deck = { go, get index() { return i; } };
})();
