"""Config loading/saving. Everything user-facing lives in JSON under config/."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
TEMPLATES_DIR = ROOT / "templates"
WORKSPACE_DIR = ROOT / "workspace"
INPUTS_DIR = WORKSPACE_DIR / "inputs"
RUNS_DIR = WORKSPACE_DIR / "runs"
DATA_DIR = ROOT / "data"

for _d in (CONFIG_DIR, TEMPLATES_DIR, INPUTS_DIR, RUNS_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)


DEFAULT_PROVIDERS: dict[str, Any] = {
    "default_provider": "anthropic",
    "providers": {
        "anthropic": {
            "type": "anthropic",
            "api_key": "",            # falls back to ANTHROPIC_API_KEY env var
            "default_model": "claude-opus-5",
            "effort": "high",         # low | medium | high | xhigh | max
            "thinking": "adaptive",   # adaptive | none
            "fallbacks": True,        # server-side refusal fallbacks (Opus 5 / Fable 5)
            "max_tokens": 16000,
            "timeout": 600,
        },
        "ollama": {
            "type": "openai",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "default_model": "llama3.2:3b",
            "temperature": 0.3,
            "max_tokens": 1500,
            "timeout": 300,
        },
        "openrouter": {
            "type": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "",
            "default_model": "anthropic/claude-sonnet-4.5",
            "fallback_model": "minimax/minimax-m3:free",
            "temperature": 0.2,
            "max_tokens": 8000,
            "timeout": 600,
        },
        "openai_compatible": {
            "type": "openai",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "default_model": "",
            "temperature": 0.2,
            "max_tokens": 4096,
            "timeout": 300,
        },
    },
}

DEFAULT_UI: dict[str, Any] = {
    "appearance": "dark",            # dark | light | system
    "accent": "#4f8cff",
    "accent_hover": "#3b6fd6",
    "font_family": "Segoe UI",
    "font_size": 13,
    "log_font_family": "Consolas",
    "log_font_size": 12,
    "corner_radius": 8,
    "window": {"width": 1320, "height": 840},
    "sidebar": {"position": "left", "width": 210, "compact": False},
    "panels": {
        "run": True, "agents": True, "workflows": True,
        "business": True, "history": True, "settings": True,
    },
    "panel_order": ["run", "agents", "workflows", "business", "history", "settings"],
    "labels": {
        "app_title": "Hermes",
        "run": "Run", "agents": "Agents", "workflows": "Workflows",
        "business": "Business", "history": "History", "settings": "Settings",
    },
    "agent_colors": {
        "hermes": "#f5c542", "system": "#9aa0a6", "tool": "#7fd1b9",
        "error": "#ff6b6b", "user": "#cfd8dc", "default": "#8ab4f8",
    },
    "show_token_usage": True,
    "show_timestamps": True,
    "log_wrap": True,
    "confirm_before_run": False,
}

DEFAULT_ORCHESTRATION: dict[str, Any] = {
    "max_iterations": 24,
    "max_delegation_depth": 2,
    "specialist_max_iterations": 8,
    "include_business_context_in_specialists": True,
}


def _path(name: str) -> Path:
    return CONFIG_DIR / f"{name}.json"


def load(name: str, default: Any) -> Any:
    p = _path(name)
    if not p.exists():
        save(name, default)
        return json.loads(json.dumps(default))
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))


def save(name: str, data: Any) -> None:
    p = _path(name)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def load_all() -> dict[str, Any]:
    from . import templates  # local import to avoid cycle
    t = templates.get("consultancy")
    return {
        "providers": load("providers", DEFAULT_PROVIDERS),
        "ui": load("ui", DEFAULT_UI),
        "orchestration": load("orchestration", DEFAULT_ORCHESTRATION),
        "business": load("business", t["business"]),
        "agents": load("agents", t["agents"]),
        "workflows": load("workflows", t["workflows"]),
    }


def resolve_api_key(pcfg: dict[str, Any]) -> str:
    key = (pcfg.get("api_key") or "").strip()
    if key:
        return key
    if pcfg.get("type") == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return os.environ.get("OPENAI_API_KEY", "")
