"""Who uses what: the links between a desk's agents and its cameras and connectors, so every tab can show them.

An agent's camera is written into its instructions when Atlas designs the team ("Your camera is 'kitchen-cam': pass
camera="kitchen-cam" to camera_ask ..."); its channels are the ones its job mentions (email, WhatsApp, SMS, Slack) and
the tools that need a connector (calendar, HTTP, MCP, Higgsfield). links() reads both from the stored roster, so the
Cameras, Integrations and Team tabs agree with what the agents will actually do, and points out the gaps: an agent
told to email a report with no way to send one, a camera nobody watches, a channel with no connector.
"""
from __future__ import annotations

import re
from typing import Any

from . import integrations as I

CAMERA_TOOLS = ("camera_ask", "camera_events", "camera_look")
# channel -> words in an agent's job that mean it delivers through that channel
CHANNEL_WORDS = {"email": r"e-?mails?", "whatsapp": r"whats\s?app", "sms": r"sms|text messages?", "instagram": r"insta\s?gram|ig dms?", "slack": r"slack"}
# tool -> connector kinds it needs (any one of them)
TOOL_KINDS = {"calendar_free_slots": ("gcal",), "calendar_book": ("gcal",), "http_request": ("http",), "mcp": ("mcp",),
              "generate_media": ("higgsfield",)}
CAMERA_LINE = "Your camera is '{cam}': pass camera=\"{cam}\" to camera_ask, camera_events and camera_look."


def _text(a: dict[str, Any]) -> str:
    ins = a.get("instructions") or []
    return " ".join([str(a.get("goal") or ""), str(a.get("role") or ""), " ".join(map(str, ins if isinstance(ins, list) else [ins]))])


def agent_cameras(a: dict[str, Any], names: list[str]) -> list[str]:
    """The cameras an agent's own job names (goal, role, instructions); [] when it may use any."""
    t = _text(a)
    return [n for n in names if re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", t, re.I)]


def links(agents: list[dict[str, Any]], cameras: list[dict[str, Any]], connectors: list[dict[str, Any]]) -> dict[str, Any]:
    """{agents: {id: {cameras, any_camera, channels: {ch: connector|None}, connectors, gaps}},
    cameras: {name: [agent ids that name it]}, any_camera: [ids], connectors: {name: [ids]}}"""
    names = [c["name"] for c in cameras]
    out_a: dict[str, Any] = {}
    by_cam: dict[str, list[str]] = {n: [] for n in names}
    by_conn: dict[str, list[str]] = {c["name"]: [] for c in connectors}
    anyc: list[str] = []
    for a in agents:
        # Atlas holds every optional tool and may look through any camera by design: listing it everywhere is noise
        if not a.get("enabled", True) or a.get("id") == "atlas":
            continue
        tools = set(a.get("granted_tools") or a.get("tools") or [])
        ent: dict[str, Any] = {"cameras": [], "any_camera": False, "channels": {}, "connectors": [], "gaps": []}
        if tools & set(CAMERA_TOOLS):
            ent["cameras"] = agent_cameras(a, names)
            ent["any_camera"] = not ent["cameras"]
            for n in ent["cameras"]:
                by_cam[n].append(a["id"])
            if ent["any_camera"]:
                anyc.append(a["id"])
        elif agent_cameras(a, names):
            ent["gaps"].append("its job names a camera but it has no camera tools")
        t = _text(a)
        for ch, pat in CHANNEL_WORDS.items():
            if not re.search(r"\b(" + pat + r")\b", t, re.I):
                continue
            conn = I.outbound_connector(connectors, ch)
            sends = "queue_action" in tools or a.get("id") == "atlas"
            ent["channels"][ch] = conn["name"] if conn else None
            if conn and sends:
                by_conn[conn["name"]].append(a["id"])
                ent["connectors"].append(conn["name"])
            if not sends:
                ent["gaps"].append(f"its job mentions {ch} but it cannot send (no queue_action tool)")
            elif not conn:
                ent["gaps"].append(f"no {ch} connector: what it queues for {ch} cannot be sent")
        for tool, kinds in TOOL_KINDS.items():
            if tool not in tools:
                continue
            hit = [c for c in connectors if c.get("kind") in kinds]
            for c in hit:
                if a["id"] not in by_conn[c["name"]]:
                    by_conn[c["name"]].append(a["id"])
                    ent["connectors"].append(c["name"])
            if not hit:
                ent["gaps"].append(f"{tool} needs a {' or '.join(kinds)} connector")
        out_a[a["id"]] = ent
    return {"agents": out_a, "cameras": by_cam, "any_camera": anyc, "connectors": by_conn}


def assign_camera(agent: dict[str, Any], camera: str, on: bool = True) -> dict[str, Any]:
    """The agent with (or without) this camera named in its instructions and the camera tools granted."""
    ins = [i for i in (agent.get("instructions") or []) if isinstance(i, str)]
    ins = [i for i in ins if not re.search(r"camera=\"" + re.escape(camera) + r"\"", i)]
    tools = list(agent.get("tools") or [])
    if on:
        ins.insert(0, CAMERA_LINE.format(cam=camera))
        tools += [t for t in CAMERA_TOOLS if t not in tools]
    return {**agent, "instructions": ins, "tools": tools}
