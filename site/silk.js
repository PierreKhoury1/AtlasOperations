/* Silk — flowing violet ribbons on a canvas. Usage: <canvas data-silk='{"ribbons":5,"alpha":1}'></canvas>
   or Silk.mount(canvas, opts). Respects prefers-reduced-motion (renders one still frame). */
(function () {
  function mount(canvas, opts) {
    const o = Object.assign({ ribbons: 5, speed: 1, alpha: 1, hue: [262, 292], maxWidth: 1400 }, opts || {});
    const ctx = canvas.getContext("2d");
    let w = 1, h = 1, t = Math.random() * 100, raf = 0;
    function size() {
      const r = canvas.getBoundingClientRect();
      const scale = Math.min(1, o.maxWidth / Math.max(1, r.width));
      w = canvas.width = Math.max(1, (r.width * scale) | 0);
      h = canvas.height = Math.max(1, (r.height * scale) | 0);
    }
    size();
    window.addEventListener("resize", size);
    const R = [];
    for (let i = 0; i < o.ribbons; i++) {
      R.push({
        y: 0.12 + (0.72 * i) / o.ribbons + Math.random() * 0.08,
        amp: 0.07 + Math.random() * 0.13,
        f: 0.6 + Math.random() * 0.7,
        ph: Math.random() * 6.283,
        th: 0.04 + Math.random() * 0.09,
        hue: o.hue[0] + Math.random() * (o.hue[1] - o.hue[0]),
        sp: 0.5 + Math.random() * 0.7,
        light: 55 + Math.random() * 20,
      });
    }
    function frame() {
      t += 0.0035 * o.speed;
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = "lighter";
      ctx.filter = "blur(" + Math.max(2, w / 240) + "px)";
      for (const r of R) {
        const N = 48, pts = [];
        for (let i = 0; i <= N; i++) {
          const x = i / N;
          const y = r.y + r.amp * Math.sin(x * r.f * 6.283 + t * r.sp * 6 + r.ph) + 0.04 * Math.sin(x * 3.3 - t * 4 + r.ph * 2);
          pts.push([x * w, y * h]);
        }
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
        for (let i = pts.length - 1; i >= 0; i--) {
          const th = r.th * h * (0.6 + 0.6 * Math.sin((pts[i][0] / w) * 6.283 + t * 3 + r.ph));
          ctx.lineTo(pts[i][0], pts[i][1] + th);
        }
        ctx.closePath();
        const g = ctx.createLinearGradient(0, 0, w, 0);
        g.addColorStop(0, "hsla(" + r.hue + ",90%," + r.light + "%,0)");
        g.addColorStop(0.35, "hsla(" + r.hue + ",90%," + r.light + "%," + 0.32 * o.alpha + ")");
        g.addColorStop(0.55, "hsla(" + (r.hue + 18) + ",95%," + (r.light + 15) + "%," + 0.6 * o.alpha + ")");
        g.addColorStop(0.75, "hsla(" + r.hue + ",90%," + r.light + "%," + 0.3 * o.alpha + ")");
        g.addColorStop(1, "hsla(" + r.hue + ",90%," + r.light + "%,0)");
        ctx.fillStyle = g;
        ctx.fill();
      }
      ctx.filter = "none";
      ctx.globalCompositeOperation = "source-over";
    }
    function loop() { frame(); raf = requestAnimationFrame(loop); }
    const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (still) frame(); else loop();
    document.addEventListener("visibilitychange", () => {
      if (still) return;
      if (document.hidden) cancelAnimationFrame(raf); else loop();
    });
    return { stop() { cancelAnimationFrame(raf); } };
  }
  window.Silk = { mount };
  document.querySelectorAll("canvas[data-silk]").forEach((c) => {
    let o = {};
    try { o = JSON.parse(c.dataset.silk || "{}"); } catch (e) {}
    mount(c, o);
  });
})();
