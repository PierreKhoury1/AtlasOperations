"""Hermes desktop UI (customtkinter). Everything visual is driven by config/ui.json."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk

from . import config as cfg
from . import templates
from . import tools as T
from .orchestrator import Event, Orchestrator
from .store import Store

PAGE_KEYS = ["run", "agents", "workflows", "business", "history", "settings"]


def open_path(p: str | Path) -> None:
    p = str(p)
    if sys.platform.startswith("win"):
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


# ---------------------------------------------------------------------------- form helper
class Form:
    """Grid-based label/widget form inside a scrollable frame; values() returns a dict."""

    def __init__(self, app: "App", master, columns: int = 1):
        self.app = app
        self.frame = ctk.CTkScrollableFrame(master, fg_color="transparent")
        self.frame.grid_columnconfigure(1, weight=1)
        self._row = 0
        self._vars: dict[str, Any] = {}
        self._texts: dict[str, ctk.CTkTextbox] = {}
        self._kinds: dict[str, str] = {}

    def _label(self, text: str):
        ctk.CTkLabel(self.frame, text=text, font=self.app.font(), anchor="w").grid(
            row=self._row, column=0, sticky="nw", padx=(4, 10), pady=6)

    def section(self, text: str):
        ctk.CTkLabel(self.frame, text=text, font=self.app.font(bold=True, delta=2), anchor="w").grid(
            row=self._row, column=0, columnspan=2, sticky="w", padx=4, pady=(14, 4))
        self._row += 1

    def entry(self, key: str, label: str, default: Any = "", secret: bool = False):
        var = tk.StringVar(value="" if default is None else str(default))
        self._label(label)
        e = ctk.CTkEntry(self.frame, textvariable=var, font=self.app.font(), show="*" if secret else "",
                         corner_radius=self.app.radius())
        e.grid(row=self._row, column=1, sticky="ew", pady=6)
        self._vars[key] = var
        self._kinds[key] = "str"
        self._row += 1
        return e

    def number(self, key: str, label: str, default: Any = 0, kind: str = "int"):
        self.entry(key, label, default)
        self._kinds[key] = kind

    def text(self, key: str, label: str, default: str = "", height: int = 120, mono: bool = False):
        self._label(label)
        tb = ctk.CTkTextbox(self.frame, height=height, wrap="word",
                            font=self.app.font(mono=mono), corner_radius=self.app.radius())
        tb.insert("1.0", default or "")
        tb.grid(row=self._row, column=1, sticky="ew", pady=6)
        self._texts[key] = tb
        self._kinds[key] = "text"
        self._row += 1
        return tb

    def switch(self, key: str, label: str, default: bool = False):
        var = tk.BooleanVar(value=bool(default))
        self._label(label)
        ctk.CTkSwitch(self.frame, text="", variable=var, progress_color=self.app.accent()).grid(
            row=self._row, column=1, sticky="w", pady=6)
        self._vars[key] = var
        self._kinds[key] = "bool"
        self._row += 1

    def option(self, key: str, label: str, values: list[str], default: str = "", command=None):
        values = values or [""]
        var = tk.StringVar(value=default if default in values else values[0])
        self._label(label)
        ctk.CTkOptionMenu(self.frame, values=values, variable=var, font=self.app.font(),
                          fg_color=self.app.accent(), button_color=self.app.accent_hover(),
                          button_hover_color=self.app.accent_hover(), command=command).grid(
            row=self._row, column=1, sticky="w", pady=6)
        self._vars[key] = var
        self._kinds[key] = "str"
        self._row += 1

    def checks(self, key: str, label: str, values: list[str], selected: list[str]):
        self._label(label)
        holder = ctk.CTkFrame(self.frame, fg_color="transparent")
        holder.grid(row=self._row, column=1, sticky="w", pady=6)
        vars_: dict[str, tk.BooleanVar] = {}
        for i, v in enumerate(values):
            var = tk.BooleanVar(value=v in selected)
            ctk.CTkCheckBox(holder, text=v, variable=var, font=self.app.font(),
                            fg_color=self.app.accent(), hover_color=self.app.accent_hover()).grid(
                row=i // 3, column=i % 3, sticky="w", padx=6, pady=2)
            vars_[v] = var
        self._vars[key] = vars_
        self._kinds[key] = "checks"
        self._row += 1

    def buttons(self, *specs: tuple[str, Callable[[], None]]):
        holder = ctk.CTkFrame(self.frame, fg_color="transparent")
        holder.grid(row=self._row, column=0, columnspan=2, sticky="w", pady=(12, 6))
        for i, (text, cmd) in enumerate(specs):
            self.app.button(holder, text, cmd).grid(row=0, column=i, padx=(0, 8))
        self._row += 1
        return holder

    def widget(self, w):
        w.grid(row=self._row, column=0, columnspan=2, sticky="ew", pady=6)
        self._row += 1

    def get(self, key: str) -> Any:
        kind = self._kinds[key]
        if kind == "text":
            return self._texts[key].get("1.0", "end").rstrip("\n")
        if kind == "checks":
            return [k for k, v in self._vars[key].items() if v.get()]
        v = self._vars[key].get()
        if kind == "bool":
            return bool(v)
        if kind == "int":
            try:
                return int(float(v))
            except ValueError:
                return 0
        if kind == "float":
            try:
                return float(v)
            except ValueError:
                return 0.0
        return v

    def values(self) -> dict[str, Any]:
        return {k: self.get(k) for k in self._kinds}


# ---------------------------------------------------------------------------- app
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.configs = cfg.load_all()
        self.store = Store()
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.orch: Orchestrator | None = None
        self.worker: threading.Thread | None = None
        self.log_buffer: list[tuple[str, str, str]] = []   # (tag, agent, text)
        self.current_deliverables: list[str] = []
        self.current_run_dir: str = ""
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.active_page = "run"
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.build()
        self.after(100, self._poll)

    # ------------------------------------------------------------ ui helpers
    @property
    def ui(self) -> dict[str, Any]:
        return self.configs["ui"]

    def font(self, bold: bool = False, delta: int = 0, mono: bool = False) -> ctk.CTkFont:
        fam = self.ui.get("log_font_family", "Consolas") if mono else self.ui.get("font_family", "Segoe UI")
        size = int(self.ui.get("log_font_size", 12) if mono else self.ui.get("font_size", 13)) + delta
        return ctk.CTkFont(family=fam, size=size, weight="bold" if bold else "normal")

    def accent(self) -> str:
        return self.ui.get("accent", "#4f8cff")

    def accent_hover(self) -> str:
        return self.ui.get("accent_hover", "#3b6fd6")

    def radius(self) -> int:
        return int(self.ui.get("corner_radius", 8))

    def label_for(self, key: str) -> str:
        return self.ui.get("labels", {}).get(key, key.title())

    def button(self, master, text: str, command, width: int = 0, secondary: bool = False, **kw):
        kw.setdefault("fg_color", ("gray75", "gray30") if secondary else self.accent())
        kw.setdefault("hover_color", ("gray65", "gray40") if secondary else self.accent_hover())
        kw.setdefault("corner_radius", self.radius())
        b = ctk.CTkButton(master, text=text, command=command, font=self.font(), **kw)
        if width:
            b.configure(width=width)
        return b

    def agent_color(self, agent_id: str) -> str:
        for a in self.configs["agents"]:
            if a["id"] == agent_id and a.get("color"):
                return a["color"]
        colors = self.ui.get("agent_colors", {})
        return colors.get(agent_id, colors.get("default", "#8ab4f8"))

    def toast(self, text: str, error: bool = False):
        self.status_var.set(text)
        self.status_label.configure(text_color=self.ui["agent_colors"].get("error", "#ff6b6b") if error else ("gray30", "gray70"))

    def confirm(self, title: str, text: str) -> bool:
        from tkinter import messagebox
        return bool(messagebox.askyesno(title, text, parent=self))

    # ------------------------------------------------------------ build / rebuild
    def build(self):
        ui = self.ui
        ctk.set_appearance_mode(ui.get("appearance", "dark"))
        self.title(self.label_for("app_title"))
        w, h = ui.get("window", {}).get("width", 1320), ui.get("window", {}).get("height", 840)
        self.geometry(f"{w}x{h}")
        self.minsize(900, 600)

        for child in list(self.winfo_children()):
            child.destroy()
        self.pages = {}

        side_pos = ui.get("sidebar", {}).get("position", "left")
        side_w = int(ui.get("sidebar", {}).get("width", 210))
        compact = bool(ui.get("sidebar", {}).get("compact", False))
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=(72 if compact else side_w), corner_radius=0)
        self.sidebar.grid(row=0, column=(0 if side_pos == "left" else 2), sticky="nsw" if side_pos == "left" else "nse")
        self.sidebar.grid_propagate(False)
        ctk.CTkLabel(self.sidebar, text=("H" if compact else self.label_for("app_title")),
                     font=self.font(bold=True, delta=8)).pack(padx=14, pady=(18, 14), anchor="w")

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        order = [k for k in ui.get("panel_order", PAGE_KEYS) if k in PAGE_KEYS] + \
                [k for k in PAGE_KEYS if k not in ui.get("panel_order", PAGE_KEYS)]
        for key in order:
            if not ui.get("panels", {}).get(key, True) and key != "settings":
                continue
            text = self.label_for(key)[:1] if compact else self.label_for(key)
            b = self.button(self.sidebar, text, lambda k=key: self.show_page(k), secondary=True, anchor="center" if compact else "w")
            b.pack(fill="x", padx=10, pady=4)
            self.nav_buttons[key] = b

        self.status_var = tk.StringVar(value="ready")
        self.status_label = ctk.CTkLabel(self.sidebar, textvariable=self.status_var, font=self.font(delta=-2),
                                         wraplength=side_w - 24, justify="left", anchor="w")
        self.status_label.pack(side="bottom", fill="x", padx=12, pady=12)

        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        self.main.grid_rowconfigure(0, weight=1)
        self.main.grid_columnconfigure(0, weight=1)

        builders = {
            "run": self.build_run, "agents": self.build_agents, "workflows": self.build_workflows,
            "business": self.build_business, "history": self.build_history, "settings": self.build_settings,
        }
        for key in order:
            if key in self.nav_buttons:
                f = ctk.CTkFrame(self.main, fg_color="transparent")
                f.grid(row=0, column=0, sticky="nsew")
                self.pages[key] = f
                builders[key](f)
        if self.active_page not in self.pages:
            self.active_page = next(iter(self.pages))
        self.show_page(self.active_page)

    def rebuild(self):
        self.build()

    def show_page(self, key: str):
        if key not in self.pages:
            return
        self.active_page = key
        self.pages[key].tkraise()
        for k, b in self.nav_buttons.items():
            b.configure(fg_color=self.accent() if k == key else ("gray75", "gray30"))
        if key == "history":
            self.refresh_history()

    # ------------------------------------------------------------ RUN page
    def build_run(self, f):
        f.grid_rowconfigure(2, weight=1)
        f.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(f, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text=f"{self.configs['business'].get('name','')} — task for Hermes",
                     font=self.font(bold=True, delta=2), anchor="w").grid(row=0, column=0, sticky="w")
        self.task_box = ctk.CTkTextbox(f, height=110, wrap="word", font=self.font(), corner_radius=self.radius())
        self.task_box.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        if getattr(self, "_last_task", ""):
            self.task_box.insert("1.0", self._last_task)

        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        modes = ["auto"] + [w["id"] for w in self.configs["workflows"]]
        self.mode_var = tk.StringVar(value=getattr(self, "_last_mode", "auto"))
        ctk.CTkOptionMenu(bar, values=modes, variable=self.mode_var, font=self.font(), width=220,
                          fg_color=self.accent(), button_color=self.accent_hover(),
                          button_hover_color=self.accent_hover()).grid(row=0, column=0, padx=(0, 8))
        self.run_btn = self.button(bar, "▶ Run", self.start_run, width=110)
        self.run_btn.grid(row=0, column=1, padx=4)
        self.stop_btn = self.button(bar, "■ Stop", self.stop_run, width=90, secondary=True)
        self.stop_btn.grid(row=0, column=2, padx=4)
        self.button(bar, "Clear log", self.clear_log, secondary=True, width=90).grid(row=0, column=3, padx=4)
        self.button(bar, "Open run folder", self.open_run_folder, secondary=True).grid(row=0, column=4, padx=4)
        self.button(bar, "Inputs folder", lambda: open_path(cfg.INPUTS_DIR), secondary=True).grid(row=0, column=5, padx=4)
        self.usage_var = tk.StringVar(value="")
        if self.ui.get("show_token_usage", True):
            ctk.CTkLabel(bar, textvariable=self.usage_var, font=self.font(delta=-1)).grid(row=0, column=6, padx=12)

        body = ctk.CTkFrame(f, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=1)
        self.log = ctk.CTkTextbox(body, wrap=("word" if self.ui.get("log_wrap", True) else "none"),
                                  font=self.font(mono=True), corner_radius=self.radius())
        self.log.grid(row=0, column=0, sticky="nsew")
        self._setup_log_tags()
        self.log.configure(state="disabled")

        right = ctk.CTkFrame(body, corner_radius=self.radius())
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="Deliverables", font=self.font(bold=True)).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
        self.deliv_frame = ctk.CTkScrollableFrame(right, fg_color="transparent")
        self.deliv_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self._render_deliverables()
        # replay buffered log
        for tag, agent, text in self.log_buffer:
            self._append_log(tag, agent, text, buffer=False)

    def _setup_log_tags(self):
        tb = self.log._textbox
        colors = self.ui.get("agent_colors", {})
        tb.tag_config("system", foreground=colors.get("system", "#9aa0a6"))
        tb.tag_config("tool", foreground=colors.get("tool", "#7fd1b9"))
        tb.tag_config("error", foreground=colors.get("error", "#ff6b6b"))
        tb.tag_config("user", foreground=colors.get("user", "#cfd8dc"))
        for a in self.configs["agents"]:
            tb.tag_config(f"agent:{a['id']}", foreground=self.agent_color(a["id"]))
        tb.tag_config("hdr", font=self.font(bold=True, mono=True))
        tb.tag_config("dim", foreground="#777777")

    def _append_log(self, tag: str, agent: str, text: str, buffer: bool = True):
        if buffer:
            self.log_buffer.append((tag, agent, text))
            if len(self.log_buffer) > 2000:
                del self.log_buffer[:500]
        if not hasattr(self, "log") or not self.log.winfo_exists():
            return
        self.log.configure(state="normal")
        stamp = time.strftime("%H:%M:%S ") if self.ui.get("show_timestamps", True) else ""
        color_tag = f"agent:{agent}" if tag == "agent" else tag
        if stamp:
            self.log.insert("end", stamp, "dim")
        self.log.insert("end", f"[{agent}] ", (color_tag, "hdr"))
        self.log.insert("end", text.rstrip() + "\n", color_tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log_buffer.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _render_deliverables(self):
        for c in list(self.deliv_frame.winfo_children()):
            c.destroy()
        if not self.current_deliverables:
            ctk.CTkLabel(self.deliv_frame, text="(none yet)", font=self.font(delta=-1)).pack(anchor="w", padx=6)
        for p in self.current_deliverables:
            self.button(self.deliv_frame, Path(p).name, lambda p=p: open_path(p), secondary=True, anchor="w").pack(fill="x", pady=2)

    def open_run_folder(self):
        open_path(self.current_run_dir or cfg.RUNS_DIR)

    def start_run(self):
        if self.worker and self.worker.is_alive():
            self.toast("a run is already in progress", error=True)
            return
        task = self.task_box.get("1.0", "end").strip()
        if not task:
            self.toast("enter a task first", error=True)
            return
        mode = self.mode_var.get()
        if self.ui.get("confirm_before_run") and not self.confirm("Run", f"Run mode '{mode}' now?"):
            return
        self._last_task, self._last_mode = task, mode
        self.current_deliverables = []
        self._render_deliverables()
        self.usage_var.set("")
        self.configs = cfg.load_all()   # pick up any saved edits
        self.orch = Orchestrator(self.configs, self.store, self.events.put)
        self._append_log("user", "owner", task)
        self.run_btn.configure(state="disabled")
        self.toast("running…")
        self.worker = threading.Thread(target=self.orch.run, args=(task, mode), daemon=True)
        self.worker.start()

    def stop_run(self):
        if self.orch:
            self.orch.cancel()
            self.toast("cancelling after current step…")

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle_event(self, ev: Event):
        k = ev.kind
        if k == "usage":
            self.usage_var.set(f"tokens in {ev.data.get('tokens_in',0):,}  out {ev.data.get('tokens_out',0):,}")
            return
        if k in ("log", "agent_start", "agent_end"):
            indent = "  " * int(ev.data.get("depth", 0))
            self._append_log("agent" if ev.agent != "system" else "system", ev.agent, indent + ev.text)
        elif k == "tool":
            self._append_log("tool", ev.agent, "⚙ " + ev.text)
        elif k == "error":
            self._append_log("error", ev.agent, "✖ " + ev.text)
            self.toast(ev.text[:120], error=True)
        elif k == "deliverable":
            p = ev.data.get("path", ev.text)
            if p not in self.current_deliverables:
                self.current_deliverables.append(p)
            self._render_deliverables()
            self._append_log("system", "deliverable", Path(p).name)
        elif k == "done":
            self.current_run_dir = ev.data.get("run_dir", "")
            self._append_log("system", "hermes", f"=== {ev.data.get('status','done').upper()} ===\n{ev.text}")
            self.run_btn.configure(state="normal")
            self.toast(f"{ev.data.get('status')} — tokens in {ev.data.get('tokens_in',0):,} / out {ev.data.get('tokens_out',0):,}")

    # ------------------------------------------------------------ AGENTS page
    def build_agents(self, f):
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(1, weight=1)
        self.agent_list = ctk.CTkScrollableFrame(f, width=230, label_text="Agents", label_font=self.font(bold=True))
        self.agent_list.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        self.agent_editor_holder = ctk.CTkFrame(f, fg_color="transparent")
        self.agent_editor_holder.grid(row=0, column=1, sticky="nsew")
        self.agent_editor_holder.grid_rowconfigure(0, weight=1)
        self.agent_editor_holder.grid_columnconfigure(0, weight=1)
        self._refresh_agent_list()
        self._edit_agent(self.configs["agents"][0]["id"] if self.configs["agents"] else None)

    def _refresh_agent_list(self):
        for c in list(self.agent_list.winfo_children()):
            c.destroy()
        for a in self.configs["agents"]:
            txt = f"{'●' if a.get('enabled', True) else '○'} {a['name']}"
            self.button(self.agent_list, txt, lambda i=a["id"]: self._edit_agent(i), secondary=True,
                        anchor="w", text_color=self.agent_color(a["id"])).pack(fill="x", pady=2)
        self.button(self.agent_list, "+ New agent", lambda: self._edit_agent(None)).pack(fill="x", pady=(10, 2))

    def _edit_agent(self, agent_id: str | None):
        for c in list(self.agent_editor_holder.winfo_children()):
            c.destroy()
        a = next((x for x in self.configs["agents"] if x["id"] == agent_id), None) or {
            "id": "", "name": "", "role": "", "enabled": True, "provider": "", "model": "",
            "tools": ["read_file", "list_files"], "system_prompt": "", "color": "#8ab4f8"}
        providers = ["(default)"] + list(self.configs["providers"].get("providers", {}).keys())
        form = Form(self, self.agent_editor_holder)
        form.frame.grid(row=0, column=0, sticky="nsew")
        form.section("Agent" if agent_id else "New agent")
        form.entry("id", "ID (unique, no spaces)", a["id"])
        form.entry("name", "Name", a["name"])
        form.entry("role", "Role (shown to Hermes)", a.get("role", ""))
        form.switch("enabled", "Enabled", a.get("enabled", True))
        form.option("provider", "Provider", providers, a.get("provider") or "(default)")
        form.entry("model", "Model (blank = provider default)", a.get("model", ""))
        form.entry("color", "Log colour (hex)", a.get("color", ""))
        form.checks("tools", "Tools", T.ALL_TOOL_NAMES, a.get("tools", []))
        form.text("system_prompt", "System prompt", a.get("system_prompt", ""), height=260)

        def save():
            v = form.values()
            v["id"] = v["id"].strip().replace(" ", "_")
            if not v["id"] or not v["name"]:
                self.toast("id and name required", error=True)
                return
            if v["provider"] == "(default)":
                v["provider"] = ""
            agents = self.configs["agents"]
            idx = next((i for i, x in enumerate(agents) if x["id"] == (agent_id or v["id"])), None)
            if idx is None:
                agents.append(v)
            else:
                agents[idx] = v
            cfg.save("agents", agents)
            self.toast(f"saved agent {v['id']}")
            self._refresh_agent_list()
            self._edit_agent(v["id"])

        def delete():
            if not agent_id or not self.confirm("Delete", f"Delete agent '{agent_id}'?"):
                return
            self.configs["agents"] = [x for x in self.configs["agents"] if x["id"] != agent_id]
            cfg.save("agents", self.configs["agents"])
            self._refresh_agent_list()
            self._edit_agent(self.configs["agents"][0]["id"] if self.configs["agents"] else None)

        def duplicate():
            v = dict(a)
            v["id"] = a["id"] + "_copy"
            v["name"] = a["name"] + " (copy)"
            self.configs["agents"].append(v)
            cfg.save("agents", self.configs["agents"])
            self._refresh_agent_list()
            self._edit_agent(v["id"])

        form.buttons(("Save", save), ("Duplicate", duplicate), ("Delete", delete))

    # ------------------------------------------------------------ WORKFLOWS page
    def build_workflows(self, f):
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(1, weight=1)
        self.wf_list = ctk.CTkScrollableFrame(f, width=230, label_text="Workflows", label_font=self.font(bold=True))
        self.wf_list.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        self.wf_holder = ctk.CTkFrame(f, fg_color="transparent")
        self.wf_holder.grid(row=0, column=1, sticky="nsew")
        self.wf_holder.grid_rowconfigure(0, weight=1)
        self.wf_holder.grid_columnconfigure(0, weight=1)
        self._refresh_wf_list()
        self._edit_wf(self.configs["workflows"][0]["id"] if self.configs["workflows"] else None)

    def _refresh_wf_list(self):
        for c in list(self.wf_list.winfo_children()):
            c.destroy()
        for w in self.configs["workflows"]:
            self.button(self.wf_list, w["name"], lambda i=w["id"]: self._edit_wf(i), secondary=True, anchor="w").pack(fill="x", pady=2)
        self.button(self.wf_list, "+ New workflow", lambda: self._edit_wf(None)).pack(fill="x", pady=(10, 2))

    def _edit_wf(self, wf_id: str | None):
        """Workflow editor: header | node graph | step editor + live JSON twin."""
        from .graph import WorkflowGraph
        for c in list(self.wf_holder.winfo_children()):
            c.destroy()
        src = next((x for x in self.configs["workflows"] if x["id"] == wf_id), None)
        draft: dict[str, Any] = json.loads(json.dumps(src)) if src else {
            "id": "", "name": "", "description": "", "synthesize": True,
            "steps": [{"agent": "research", "task": "Research:\n{task}"}]}
        agents = {a["id"]: a for a in self.configs["agents"]}
        specialist_ids = [a for a in agents if a != "hermes"] or list(agents) or ["research"]
        sel: dict[str, int | None] = {"i": 0 if draft["steps"] else None}

        h = self.wf_holder
        h.grid_rowconfigure(2, weight=1)
        h.grid_columnconfigure(0, weight=1)

        # ---- header row
        head = ctk.CTkFrame(h, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(5, weight=1)
        v_id = tk.StringVar(value=draft.get("id", ""))
        v_name = tk.StringVar(value=draft.get("name", ""))
        v_desc = tk.StringVar(value=draft.get("description", ""))
        v_syn = tk.BooleanVar(value=bool(draft.get("synthesize", True)))
        for col, (lbl, var, w) in enumerate((("ID", v_id, 150), ("Name", v_name, 200), ("Description", v_desc, 240))):
            ctk.CTkLabel(head, text=lbl, font=self.font()).grid(row=0, column=col * 2, padx=(0, 6))
            e = ctk.CTkEntry(head, textvariable=var, font=self.font(), corner_radius=self.radius())
            if w:
                e.configure(width=w)
            e.grid(row=0, column=col * 2 + 1, sticky="ew", padx=(0, 14))

        def toggle_syn():
            draft["synthesize"] = v_syn.get()
            refresh()
        ctk.CTkSwitch(head, text="Hermes synthesis at end", variable=v_syn, command=toggle_syn,
                      font=self.font(), progress_color=self.accent()).grid(row=0, column=6, padx=6)

        # ---- graph
        def on_select(idx: int | None):
            sel["i"] = idx
            load_step()
        graph = WorkflowGraph(h, self, on_select, corner_radius=self.radius())
        graph.grid(row=1, column=0, sticky="ew", pady=8)

        # ---- bottom: step editor | JSON twin
        bottom = ctk.CTkFrame(h, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="nsew")
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_columnconfigure(1, weight=1)
        bottom.grid_rowconfigure(2, weight=1)

        left = ctk.CTkFrame(bottom, corner_radius=self.radius())
        left.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 6))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(3, weight=1)
        step_title = tk.StringVar(value="Step")
        ctk.CTkLabel(left, textvariable=step_title, font=self.font(bold=True)).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        arow = ctk.CTkFrame(left, fg_color="transparent")
        arow.grid(row=1, column=0, sticky="ew", padx=10)
        ctk.CTkLabel(arow, text="Agent", font=self.font()).grid(row=0, column=0, padx=(0, 8))
        v_agent = tk.StringVar(value=specialist_ids[0])
        ctk.CTkOptionMenu(arow, values=specialist_ids, variable=v_agent, font=self.font(), fg_color=self.accent(),
                          button_color=self.accent_hover(), button_hover_color=self.accent_hover()).grid(row=0, column=1)
        ctk.CTkLabel(left, text="Task template — placeholders: {task} {previous} {all}", font=self.font(delta=-1)).grid(
            row=2, column=0, sticky="w", padx=10, pady=(8, 2))
        task_box = ctk.CTkTextbox(left, wrap="word", font=self.font(), corner_radius=self.radius())
        task_box.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 6))
        brow = ctk.CTkFrame(left, fg_color="transparent")
        brow.grid(row=4, column=0, sticky="w", padx=10, pady=(0, 10))

        right = ctk.CTkFrame(bottom, corner_radius=self.radius())
        right.grid(row=0, column=1, rowspan=3, sticky="nsew", padx=(6, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(right, text="JSON (graph ⇄ json)", font=self.font(bold=True)).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        json_box = ctk.CTkTextbox(right, wrap="word", font=self.font(mono=True), corner_radius=self.radius())
        json_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))

        # ---- sync helpers
        def collect_header():
            draft["id"] = v_id.get().strip().replace(" ", "_")
            draft["name"] = v_name.get().strip()
            draft["description"] = v_desc.get().strip()
            draft["synthesize"] = v_syn.get()

        def refresh():
            collect_header()
            if sel["i"] is not None and sel["i"] >= len(draft["steps"]):
                sel["i"] = len(draft["steps"]) - 1 if draft["steps"] else None
            graph.set_workflow(draft, agents, sel["i"])
            json_box.delete("1.0", "end")
            json_box.insert("1.0", json.dumps(draft, indent=2, ensure_ascii=False))
            load_step()

        def load_step():
            i = sel["i"]
            task_box.delete("1.0", "end")
            if i is None or not draft["steps"]:
                step_title.set("No step selected — click a node or add one")
                return
            s = draft["steps"][i]
            step_title.set(f"Step {i + 1} of {len(draft['steps'])}")
            v_agent.set(s.get("agent", specialist_ids[0]))
            task_box.insert("1.0", s.get("task", ""))

        def apply_step():
            i = sel["i"]
            if i is None:
                return add_step()
            draft["steps"][i] = {"agent": v_agent.get(), "task": task_box.get("1.0", "end").rstrip("\n")}
            refresh()

        def add_step():
            i = sel["i"]
            new = {"agent": v_agent.get() or specialist_ids[0], "task": "{task}\n\nPrevious output:\n{previous}"}
            pos = len(draft["steps"]) if i is None else i + 1
            draft["steps"].insert(pos, new)
            sel["i"] = pos
            refresh()

        def delete_step():
            i = sel["i"]
            if i is None:
                return
            del draft["steps"][i]
            sel["i"] = min(i, len(draft["steps"]) - 1) if draft["steps"] else None
            refresh()

        def move(delta: int):
            i = sel["i"]
            if i is None:
                return
            j = i + delta
            if 0 <= j < len(draft["steps"]):
                draft["steps"][i], draft["steps"][j] = draft["steps"][j], draft["steps"][i]
                sel["i"] = j
                refresh()

        def apply_json():
            try:
                data = json.loads(json_box.get("1.0", "end"))
                steps = data["steps"] if isinstance(data, dict) else data
                assert isinstance(steps, list) and all(isinstance(s, dict) and "agent" in s and "task" in s for s in steps), \
                    "steps must be a list of {agent, task}"
            except Exception as exc:
                self.toast(f"JSON invalid: {exc}", error=True)
                return
            if isinstance(data, dict):
                v_id.set(str(data.get("id", v_id.get())))
                v_name.set(str(data.get("name", v_name.get())))
                v_desc.set(str(data.get("description", v_desc.get())))
                v_syn.set(bool(data.get("synthesize", v_syn.get())))
            draft["steps"] = [{"agent": s["agent"], "task": s["task"]} for s in steps]
            sel["i"] = 0 if draft["steps"] else None
            refresh()
            self.toast("JSON applied to graph")

        for text, cmd in (("Apply step", apply_step), ("+ Add after", add_step), ("Delete", delete_step),
                          ("◀", lambda: move(-1)), ("▶", lambda: move(1))):
            self.button(brow, text, cmd, secondary=text not in ("Apply step",), width=(40 if len(text) == 1 else 0)).pack(side="left", padx=(0, 6))
        self.button(right, "Apply JSON → graph", apply_json).grid(row=2, column=0, sticky="w", padx=10, pady=(0, 10))

        # ---- footer
        def save():
            collect_header()
            if not draft["id"] or not draft["name"]:
                self.toast("id and name required", error=True)
                return
            unknown = [s["agent"] for s in draft["steps"] if s["agent"] not in agents]
            if unknown:
                self.toast(f"unknown agent(s): {', '.join(unknown)}", error=True)
                return
            wfs = self.configs["workflows"]
            idx = next((i for i, x in enumerate(wfs) if x["id"] == (wf_id or draft["id"])), None)
            if idx is None:
                wfs.append(draft)
            else:
                wfs[idx] = draft
            cfg.save("workflows", wfs)
            self.toast(f"saved workflow {draft['id']}")
            self._refresh_wf_list()
            self._edit_wf(draft["id"])
            if "run" in self.pages:
                self.rebuild_page("run")

        def delete():
            if not wf_id or not self.confirm("Delete", f"Delete workflow '{wf_id}'?"):
                return
            self.configs["workflows"] = [x for x in self.configs["workflows"] if x["id"] != wf_id]
            cfg.save("workflows", self.configs["workflows"])
            self._refresh_wf_list()
            self._edit_wf(self.configs["workflows"][0]["id"] if self.configs["workflows"] else None)
            if "run" in self.pages:
                self.rebuild_page("run")

        self.button(head, "Save", save, width=80).grid(row=0, column=7, padx=(12, 4))
        self.button(head, "Delete", delete, width=80, secondary=True).grid(row=0, column=8)
        refresh()

    def rebuild_page(self, key: str):
        if key not in self.pages:
            return
        for c in list(self.pages[key].winfo_children()):
            c.destroy()
        {"run": self.build_run, "agents": self.build_agents, "workflows": self.build_workflows,
         "business": self.build_business, "history": self.build_history, "settings": self.build_settings}[key](self.pages[key])

    # ------------------------------------------------------------ BUSINESS page
    def build_business(self, f):
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        b = self.configs["business"]
        form = Form(self, f)
        form.frame.grid(row=0, column=0, sticky="nsew")
        form.section("Business model template")
        form.option("template", "Load template (replaces business, agents, workflows)", templates.names(), b.get("model", "consultancy"))
        form.entry("template_name", "Save current setup as template named", "")

        def apply_template():
            name = form.get("template")
            if not self.confirm("Apply template", f"Replace business profile, agents and workflows with '{name}'?"):
                return
            t = templates.get(name)
            for k in ("business", "agents", "workflows"):
                self.configs[k] = t[k]
                cfg.save(k, t[k])
            self.toast(f"template '{name}' applied")
            self.rebuild()
            self.show_page("business")

        def save_template():
            name = form.get("template_name").strip().replace(" ", "_")
            if not name:
                self.toast("template name required", error=True)
                return
            templates.export_current(name, self._business_values(form), self.configs["agents"], self.configs["workflows"])
            self.toast(f"saved template '{name}'")

        form.buttons(("Apply template", apply_template), ("Save as template", save_template))
        form.section("Profile")
        form.entry("name", "Business name", b.get("name", ""))
        form.entry("model", "Business model", b.get("model", ""))
        form.entry("tagline", "Tagline", b.get("tagline", ""))
        form.text("description", "Description", b.get("description", ""), height=80)
        form.text("services", "Services (one per line)", "\n".join(b.get("services", [])), height=110)
        form.entry("target_clients", "Target clients", b.get("target_clients", ""))
        form.entry("tone", "Tone of voice", b.get("tone", ""))
        form.entry("currency", "Currency", b.get("currency", ""))
        form.entry("pricing_notes", "Pricing notes", b.get("pricing_notes", ""))
        form.text("extra_context", "Extra context for all agents", b.get("extra_context", ""), height=140)

        def save():
            self.configs["business"] = self._business_values(form)
            cfg.save("business", self.configs["business"])
            self.toast("business profile saved")
            self.rebuild_page("run")

        form.buttons(("Save profile", save))

    @staticmethod
    def _business_values(form: Form) -> dict[str, Any]:
        v = form.values()
        return {
            "name": v["name"], "model": v["model"], "tagline": v["tagline"], "description": v["description"],
            "services": [s.strip() for s in v["services"].splitlines() if s.strip()],
            "target_clients": v["target_clients"], "tone": v["tone"], "currency": v["currency"],
            "pricing_notes": v["pricing_notes"], "extra_context": v["extra_context"],
        }

    # ------------------------------------------------------------ HISTORY page
    def build_history(self, f):
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(1, weight=1)
        self.hist_list = ctk.CTkScrollableFrame(f, width=340, label_text="Runs", label_font=self.font(bold=True))
        self.hist_list.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        right = ctk.CTkFrame(f, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew")
        self._hist_sel: dict[str, Any] | None = None
        self.button(bar, "Open folder", lambda: self._hist_sel and open_path(self._hist_sel["run_dir"]), secondary=True).grid(row=0, column=0, padx=(0, 6))
        self.button(bar, "Re-run task", self._hist_rerun, secondary=True).grid(row=0, column=1, padx=6)
        self.button(bar, "Delete", self._hist_delete, secondary=True).grid(row=0, column=2, padx=6)
        self.button(bar, "Refresh", self.refresh_history, secondary=True).grid(row=0, column=3, padx=6)
        self.hist_detail = ctk.CTkTextbox(right, wrap="word", font=self.font(mono=True), corner_radius=self.radius())
        self.hist_detail.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.refresh_history()

    def refresh_history(self):
        if not hasattr(self, "hist_list") or not self.hist_list.winfo_exists():
            return
        for c in list(self.hist_list.winfo_children()):
            c.destroy()
        for r in self.store.runs():
            when = time.strftime("%d %b %H:%M", time.localtime(r["created"]))
            txt = f"{when}  {r['status']:<9} {r['mode']}\n{r['task'][:70]}"
            self.button(self.hist_list, txt, lambda r=r: self._hist_show(r), secondary=True, anchor="w").pack(fill="x", pady=2)

    def _hist_show(self, r: dict[str, Any]):
        self._hist_sel = r
        self.hist_detail.configure(state="normal")
        self.hist_detail.delete("1.0", "end")
        self.hist_detail.insert("end", f"run {r['id']}  {r['status']}  mode={r['mode']}  tokens in {r['tokens_in']:,} / out {r['tokens_out']:,}\n\n")
        self.hist_detail.insert("end", f"TASK\n{r['task']}\n\nSUMMARY\n{r['summary']}\n\nEVENTS\n")
        for e in self.store.events(r["id"]):
            self.hist_detail.insert("end", f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} [{e['agent']}] {e['kind']}: {e['text'][:400]}\n")
        self.hist_detail.configure(state="disabled")

    def _hist_rerun(self):
        if not self._hist_sel or "run" not in self.pages:
            return
        self.task_box.delete("1.0", "end")
        self.task_box.insert("1.0", self._hist_sel["task"])
        self.mode_var.set(self._hist_sel["mode"] if self._hist_sel["mode"] in ["auto"] + [w["id"] for w in self.configs["workflows"]] else "auto")
        self.show_page("run")

    def _hist_delete(self):
        if self._hist_sel and self.confirm("Delete", "Delete this run from history? (files stay on disk)"):
            self.store.delete_run(self._hist_sel["id"])
            self._hist_sel = None
            self.refresh_history()

    # ------------------------------------------------------------ SETTINGS page
    def build_settings(self, f):
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        tabs = ctk.CTkTabview(f, corner_radius=self.radius(), segmented_button_selected_color=self.accent(),
                              segmented_button_selected_hover_color=self.accent_hover())
        tabs.grid(row=0, column=0, sticky="nsew")
        for name in ("Providers", "Orchestration", "Appearance", "Layout & labels"):
            tabs.add(name)
            tabs.tab(name).grid_rowconfigure(0, weight=1)
            tabs.tab(name).grid_columnconfigure(0, weight=1)
        self._settings_providers(tabs.tab("Providers"))
        self._settings_orch(tabs.tab("Orchestration"))
        self._settings_appearance(tabs.tab("Appearance"))
        self._settings_layout(tabs.tab("Layout & labels"))

    def _settings_providers(self, f):
        pc = self.configs["providers"]
        an = pc["providers"].get("anthropic", cfg.DEFAULT_PROVIDERS["providers"]["anthropic"])
        oa = pc["providers"].get("openai_compatible", cfg.DEFAULT_PROVIDERS["providers"]["openai_compatible"])
        form = Form(self, f)
        form.frame.grid(row=0, column=0, sticky="nsew")
        form.option("default_provider", "Default provider", ["anthropic", "openai_compatible"], pc.get("default_provider", "anthropic"))
        form.section("Anthropic (Claude)")
        form.entry("an_key", "API key (blank = ANTHROPIC_API_KEY env)", an.get("api_key", ""), secret=True)
        form.entry("an_model", "Default model", an.get("default_model", "claude-opus-5"))
        form.option("an_effort", "Effort", ["low", "medium", "high", "xhigh", "max"], an.get("effort", "high"))
        form.option("an_thinking", "Thinking", ["adaptive", "none"], an.get("thinking", "adaptive"))
        form.switch("an_fallbacks", "Server-side refusal fallbacks (Opus 5 / Fable 5)", an.get("fallbacks", True))
        form.number("an_max_tokens", "Max output tokens", an.get("max_tokens", 16000))
        form.number("an_timeout", "Timeout (s)", an.get("timeout", 600))
        form.section("OpenAI-compatible (Ollama / LM Studio / vLLM / OpenRouter / OpenAI)")
        form.entry("oa_base", "Base URL (…/v1)", oa.get("base_url", ""))
        form.entry("oa_key", "API key", oa.get("api_key", ""), secret=True)
        form.entry("oa_model", "Default model", oa.get("default_model", ""))
        form.number("oa_temp", "Temperature", oa.get("temperature", 0.2), kind="float")
        form.number("oa_max_tokens", "Max output tokens", oa.get("max_tokens", 4096))
        form.number("oa_timeout", "Timeout (s)", oa.get("timeout", 300))

        def collect() -> dict[str, Any]:
            v = form.values()
            return {
                "default_provider": v["default_provider"],
                "providers": {
                    "anthropic": {"type": "anthropic", "api_key": v["an_key"], "default_model": v["an_model"],
                                  "effort": v["an_effort"], "thinking": v["an_thinking"], "fallbacks": v["an_fallbacks"],
                                  "max_tokens": v["an_max_tokens"], "timeout": v["an_timeout"]},
                    "openai_compatible": {"type": "openai", "base_url": v["oa_base"], "api_key": v["oa_key"],
                                          "default_model": v["oa_model"], "temperature": v["oa_temp"],
                                          "max_tokens": v["oa_max_tokens"], "timeout": v["oa_timeout"]},
                },
            }

        def save():
            self.configs["providers"] = collect()
            cfg.save("providers", self.configs["providers"])
            self.toast("providers saved (key stored in config/providers.json)")

        def test(name: str):
            from .providers import make_provider
            pcfg = collect()["providers"][name]
            self.toast(f"testing {name}…")

            def work():
                try:
                    msg = make_provider(pcfg).test()
                    self.after(0, lambda: self.toast(f"{name}: {msg}"))
                except Exception as exc:
                    self.after(0, lambda: self.toast(f"{name}: {type(exc).__name__}: {exc}", error=True))
            threading.Thread(target=work, daemon=True).start()

        form.buttons(("Save", save), ("Test Anthropic", lambda: test("anthropic")),
                     ("Test OpenAI-compatible", lambda: test("openai_compatible")))

    def _settings_orch(self, f):
        o = self.configs["orchestration"]
        form = Form(self, f)
        form.frame.grid(row=0, column=0, sticky="nsew")
        form.number("max_iterations", "Hermes max iterations", o.get("max_iterations", 24))
        form.number("specialist_max_iterations", "Specialist max iterations", o.get("specialist_max_iterations", 8))
        form.number("max_delegation_depth", "Max delegation depth", o.get("max_delegation_depth", 2))
        form.switch("include_business_context_in_specialists", "Give specialists the business profile", o.get("include_business_context_in_specialists", True))

        def save():
            self.configs["orchestration"] = form.values()
            cfg.save("orchestration", self.configs["orchestration"])
            self.toast("orchestration saved")
        form.buttons(("Save", save))

    def _settings_appearance(self, f):
        u = self.ui
        form = Form(self, f)
        form.frame.grid(row=0, column=0, sticky="nsew")
        form.option("appearance", "Appearance", ["dark", "light", "system"], u.get("appearance", "dark"))
        form.entry("accent", "Accent colour (hex)", u.get("accent"))
        form.entry("accent_hover", "Accent hover colour (hex)", u.get("accent_hover"))
        form.entry("font_family", "Font family", u.get("font_family"))
        form.number("font_size", "Font size", u.get("font_size", 13))
        form.entry("log_font_family", "Log font family", u.get("log_font_family"))
        form.number("log_font_size", "Log font size", u.get("log_font_size", 12))
        form.number("corner_radius", "Corner radius", u.get("corner_radius", 8))
        form.section("Agent log colours (hex)")
        ac = u.get("agent_colors", {})
        for k in ("system", "tool", "error", "user", "default"):
            form.entry(f"col_{k}", k, ac.get(k, ""))
        form.section("Run log")
        form.switch("show_token_usage", "Show token usage", u.get("show_token_usage", True))
        form.switch("show_timestamps", "Show timestamps", u.get("show_timestamps", True))
        form.switch("log_wrap", "Wrap log lines", u.get("log_wrap", True))
        form.switch("confirm_before_run", "Confirm before each run", u.get("confirm_before_run", False))

        def apply(save_too: bool):
            v = form.values()
            for k in ("appearance", "accent", "accent_hover", "font_family", "font_size", "log_font_family",
                      "log_font_size", "corner_radius", "show_token_usage", "show_timestamps", "log_wrap", "confirm_before_run"):
                u[k] = v[k]
            u.setdefault("agent_colors", {})
            for k in ("system", "tool", "error", "user", "default"):
                if v[f"col_{k}"]:
                    u["agent_colors"][k] = v[f"col_{k}"]
            if save_too:
                cfg.save("ui", u)
            self.active_page = "settings"
            self.rebuild()
            self.toast("appearance applied" + (" + saved" if save_too else ""))

        def reset():
            if self.confirm("Reset", "Reset all UI settings to defaults?"):
                self.configs["ui"] = json.loads(json.dumps(cfg.DEFAULT_UI))
                cfg.save("ui", self.configs["ui"])
                self.rebuild()

        form.buttons(("Apply", lambda: apply(False)), ("Apply + Save", lambda: apply(True)), ("Reset UI defaults", reset))

    def _settings_layout(self, f):
        u = self.ui
        form = Form(self, f)
        form.frame.grid(row=0, column=0, sticky="nsew")
        form.section("Window & sidebar")
        form.number("win_w", "Window width", u.get("window", {}).get("width", 1320))
        form.number("win_h", "Window height", u.get("window", {}).get("height", 840))
        form.option("side_pos", "Sidebar position", ["left", "right"], u.get("sidebar", {}).get("position", "left"))
        form.number("side_w", "Sidebar width", u.get("sidebar", {}).get("width", 210))
        form.switch("side_compact", "Compact sidebar (icons only)", u.get("sidebar", {}).get("compact", False))
        form.section("Panels (Settings always visible)")
        for k in PAGE_KEYS:
            if k != "settings":
                form.switch(f"panel_{k}", k, u.get("panels", {}).get(k, True))
        form.entry("panel_order", "Panel order (comma separated)", ", ".join(u.get("panel_order", PAGE_KEYS)))
        form.section("Labels")
        form.entry("lbl_app_title", "App title", u.get("labels", {}).get("app_title", "Hermes"))
        for k in PAGE_KEYS:
            form.entry(f"lbl_{k}", f"Label: {k}", u.get("labels", {}).get(k, k.title()))

        def apply(save_too: bool):
            v = form.values()
            u["window"] = {"width": max(900, v["win_w"]), "height": max(600, v["win_h"])}
            u["sidebar"] = {"position": v["side_pos"], "width": max(60, v["side_w"]), "compact": v["side_compact"]}
            u["panels"] = {k: (True if k == "settings" else v[f"panel_{k}"]) for k in PAGE_KEYS}
            order = [s.strip() for s in v["panel_order"].split(",") if s.strip() in PAGE_KEYS]
            u["panel_order"] = order + [k for k in PAGE_KEYS if k not in order]
            u["labels"] = {"app_title": v["lbl_app_title"] or "Hermes", **{k: v[f"lbl_{k}"] or k.title() for k in PAGE_KEYS}}
            if save_too:
                cfg.save("ui", u)
            self.active_page = "settings"
            self.rebuild()
            self.toast("layout applied" + (" + saved" if save_too else ""))

        form.buttons(("Apply", lambda: apply(False)), ("Apply + Save", lambda: apply(True)),
                     ("Open config folder", lambda: open_path(cfg.CONFIG_DIR)))

    # ------------------------------------------------------------ close
    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not self.confirm("Quit", "A run is in progress. Quit anyway?"):
                return
            if self.orch:
                self.orch.cancel()
        self.destroy()


def main():
    ctk.set_default_color_theme("blue")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
