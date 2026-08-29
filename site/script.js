// ---- brand config: change once here ----
const BRAND = { name: "Hermes Ops", email: "hello@hermesops.co" };

document.querySelectorAll("[data-brand]").forEach(el => (el.textContent = BRAND.name));
document.querySelectorAll("[data-email]").forEach(el => { el.textContent = BRAND.email; el.href = "mailto:" + BRAND.email; });
document.getElementById("yr").textContent = new Date().getFullYear();

// mobile menu
const burger = document.getElementById("burger"), menu = document.getElementById("menu");
burger?.addEventListener("click", () => menu.classList.toggle("open"));
menu?.querySelectorAll("a").forEach(a => a.addEventListener("click", () => menu.classList.remove("open")));

// scoping form -> mailto (no backend needed; swap for a POST when hosted)
document.getElementById("scopeForm")?.addEventListener("submit", ev => {
  ev.preventDefault();
  const f = new FormData(ev.target), g = k => (f.get(k) || "").toString().trim();
  const subject = `Scoping call — ${g("company")} (${g("desk")})`;
  const body = [
    `Name: ${g("name")}`, `Company: ${g("company")}`, `Email: ${g("email")}`, `Phone: ${g("phone")}`,
    `Desk: ${g("desk")}`, "", "Current process:", g("notes"),
  ].join("\n");
  window.location.href = `mailto:${BRAND.email}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  document.getElementById("formNote").textContent = "Your email client should open now. If not, email us directly.";
});

