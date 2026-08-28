"""Workflow node graph (tk.Canvas) — visual twin of the workflow JSON.

Nodes: TASK input -> step nodes (one per JSON step, coloured by agent) -> optional HERMES synthesis -> OUTPUT.
Edges: solid = sequential ({previous}); dashed = fan-in from earlier steps when a task uses {all}.
Click a node to select it (callback gets the step index, or None for non-step nodes).
"""
from __future__ import annotations

import tkinter as tk
from typing import Any, Callable

import customtkinter as ctk

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 190, 78, 46, 34, 24


class WorkflowGraph(ctk.CTkFrame):
    def __init__(self, master, app, on_select: Callable[[int | None], None], **kw):
        super().__init__(master, **kw)
        self.app = app
        self.on_select = on_select
        self.selected: int | None = None
        self.wf: dict[str, Any] = {"steps": []}
        self.agents: dict[str, dict[str, Any]] = {}
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.hbar = ctk.CTkScrollbar(self, orientation="horizontal", command=self.canvas.xview)
        self.hbar.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=self.hbar.set)
        self.canvas.bind("<Configure>", lambda e: self.render())
        self.canvas.bind("<Button-1>", self._click)
        self._hit: list[tuple[tuple[float, float, float, float], int | None]] = []

    # ------------------------------------------------------------------ theme
    def _c(self, light: str, dark: str) -> str:
        return dark if ctk.get_appearance_mode() == "Dark" else light

    def set_workflow(self, wf: dict[str, Any], agents: dict[str, dict[str, Any]], selected: int | None = None):
        self.wf = wf
        self.agents = agents
        self.selected = selected
        self.render()

    # ------------------------------------------------------------------ layout
    def _nodes(self) -> list[dict[str, Any]]:
        nodes = [{"kind": "input", "title": "TASK", "sub": "owner input", "color": self._c("#607d8b", "#78909c"), "idx": None}]
        for i, s in enumerate(self.wf.get("steps", [])):
            a = self.agents.get(s.get("agent", ""), {})
            nodes.append({
                "kind": "step", "idx": i,
                "title": (f"{i + 1}. {a.get('name', s.get('agent', '?'))}")[:22],
                "sub": (s.get("task", "").strip().splitlines() or [""])[0][:30],
                "color": a.get("color") or self.app.agent_color(s.get("agent", "")),
                "uses_all": "{all}" in s.get("task", ""),
                "uses_prev": "{previous}" in s.get("task", ""),
                "missing": s.get("agent", "") not in self.agents,
            })
        if self.wf.get("synthesize"):
            h = self.agents.get("hermes", {})
            nodes.append({"kind": "synth", "idx": None, "title": "Hermes synthesis",
                          "sub": "final deliverable(s)", "color": h.get("color") or "#f5c542", "uses_all": True})
        nodes.append({"kind": "output", "idx": None, "title": "OUTPUT", "sub": "workspace/runs/<id>/",
                      "color": self._c("#43a047", "#66bb6a")})
        return nodes

    def render(self):
        cv = self.canvas
        cv.delete("all")
        bg = self.app._apply_appearance_mode(ctk.ThemeManager.theme["CTkFrame"]["fg_color"])
        cv.configure(bg=bg)
        fg = self._c("#1a1a1a", "#f0f0f0")
        dim = self._c("#555555", "#aaaaaa")
        edge = self._c("#7a7a7a", "#8a8a8a")
        nodes = self._nodes()
        width = max(cv.winfo_width(), 200)
        per_row = max(1, (width - 2 * PAD + GAP_X) // (NODE_W + GAP_X))
        pos: list[tuple[float, float]] = []
        for n, _ in enumerate(nodes):
            r, c = divmod(n, per_row)
            if r % 2 == 1:  # snake layout so arrows keep flowing
                c = per_row - 1 - c
            pos.append((PAD + c * (NODE_W + GAP_X), PAD + r * (NODE_H + GAP_Y)))
        font = (self.app.ui.get("font_family", "Segoe UI"), int(self.app.ui.get("font_size", 13)))
        small = (font[0], max(8, font[1] - 3))
        bold = (font[0], font[1], "bold")

        # edges: sequential
        for n in range(1, len(nodes)):
            self._arrow(pos[n - 1], pos[n], edge, dashed=False)
        # edges: {all} fan-in from every earlier step node
        step_positions = [(i, pos[i]) for i, nd in enumerate(nodes) if nd["kind"] == "step"]
        for n, nd in enumerate(nodes):
            if nd.get("uses_all"):
                for i, p in step_positions:
                    if i < n - 1:
                        self._arrow(p, pos[n], edge, dashed=True)

        self._hit = []
        for n, nd in enumerate(nodes):
            x, y = pos[n]
            sel = nd["kind"] == "step" and nd["idx"] == self.selected
            outline = self.app.accent() if sel else nd["color"]
            self._round_rect(x, y, x + NODE_W, y + NODE_H, 12, fill=self._c("#ffffff", "#2b2b2b"),
                             outline=outline, width=3 if sel else 2)
            cv.create_rectangle(x, y + 2, x + 8, y + NODE_H - 2, fill=nd["color"], outline=nd["color"])
            cv.create_text(x + 16, y + 14, text=nd["title"], anchor="nw", fill=fg, font=bold)
            cv.create_text(x + 16, y + 38, text=nd["sub"], anchor="nw", fill=dim, font=small)
            if nd["kind"] == "step":
                tag = "{all}" if nd.get("uses_all") else ("{previous}" if nd.get("uses_prev") else "{task}")
                cv.create_text(x + NODE_W - 10, y + NODE_H - 10, text=tag, anchor="se", fill=dim, font=small)
                if nd.get("missing"):
                    cv.create_text(x + 16, y + NODE_H - 10, text="⚠ unknown agent", anchor="sw", fill="#ff6b6b", font=small)
            self._hit.append(((x, y, x + NODE_W, y + NODE_H), nd["idx"] if nd["kind"] == "step" else None))

        rows = (len(nodes) + per_row - 1) // per_row
        total_w = PAD * 2 + per_row * (NODE_W + GAP_X) - GAP_X
        total_h = PAD * 2 + rows * (NODE_H + GAP_Y) - GAP_Y
        cv.configure(scrollregion=(0, 0, max(total_w, width), total_h))
        cv.configure(height=min(max(total_h, NODE_H + 2 * PAD), 4 * (NODE_H + GAP_Y)))

    def _arrow(self, a, b, color, dashed):
        ax, ay = a[0] + NODE_W / 2, a[1] + NODE_H / 2
        bx, by = b[0] + NODE_W / 2, b[1] + NODE_H / 2
        same_row = abs(ay - by) < 1
        if same_row:
            if bx > ax:
                pts = (a[0] + NODE_W, ay, b[0], by)
            else:
                pts = (a[0], ay, b[0] + NODE_W, by)
        else:
            pts = (ax, a[1] + NODE_H, bx, b[1])
        kw = {"fill": color, "arrow": tk.LAST, "width": 2, "smooth": True}
        if dashed:
            kw.update({"dash": (4, 4), "width": 1})
            # arc above the row so fan-in edges don't hide under the sequential ones
            sx, sy = ax, a[1]
            ex, ey = bx - NODE_W * 0.3, b[1]
            lift = 18 + 6 * (abs(ex - sx) / (NODE_W + GAP_X))
            pts = (sx, sy, (sx + ex) / 2, min(sy, ey) - lift, ex, ey)
        self.canvas.create_line(*pts, **kw)

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
               x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _click(self, ev):
        x, y = self.canvas.canvasx(ev.x), self.canvas.canvasy(ev.y)
        for (x1, y1, x2, y2), idx in self._hit:
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.selected = idx
                self.render()
                self.on_select(idx)
                return
