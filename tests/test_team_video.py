"""Dynamic team assembly, the Hermes-lead text protocol, and video understanding."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from atlas import vision
from atlas.orchestrator import Orchestrator
from atlas.providers import ToolCall


def _configs():
    return {
        "providers": {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0}}},
        "orchestration": {"max_iterations": 6, "specialist_max_iterations": 4, "max_delegation_depth": 2,
                          "max_team_agents": 3},
        "business": {"name": "Testco", "model": "consultancy", "services": [], "sender_name": "Pierre"},
        "agents": [
            {"id": "atlas", "name": "Atlas", "role": "lead", "system_prompt": "lead", "provider": "demo",
             "tools": ["delegate", "assemble_team", "list_agents", "finish"], "enabled": True},
        ],
        "workflows": [],
    }


def _orch(tmp_path):
    o = Orchestrator(_configs(), None, lambda ev: None)
    o.run_dir = tmp_path
    o.run_id = "test"
    return o


class TestAssembleTeam:
    def test_registers_agents_and_delegate_works(self, tmp_path):
        o = _orch(tmp_path)
        atlas = o.agents["atlas"]
        out = o._tool(atlas, ToolCall("1", "assemble_team", {"agents": [
            {"id": "researcher2", "name": "Researcher", "role": "digs facts",
             "system_prompt": "You research.", "tools": ["web_fetch", "finish", "assemble_team"]},
        ], "reason": "test"}), 0)
        assert "researcher2" in out and "Team ready" in out
        a = o.agents["researcher2"]
        assert a["dynamic"] is True
        assert "web_fetch" in a["tools"]
        assert "finish" not in a["tools"] and "assemble_team" not in a["tools"]  # denied grants stripped
        reply = o._tool(atlas, ToolCall("2", "delegate", {"agent_id": "researcher2", "task": "look into Acme"}), 0)
        assert reply and "ERROR" not in reply

    def test_team_size_cap(self, tmp_path):
        o = _orch(tmp_path)
        specs = [{"id": f"a{i}", "name": f"A{i}", "role": "r", "system_prompt": "s"} for i in range(4)]
        out = o._tool(o.agents["atlas"], ToolCall("1", "assemble_team", {"agents": specs}), 0)
        assert "too large" in out

    def test_rejects_reserved_id(self, tmp_path):
        o = _orch(tmp_path)
        out = o._tool(o.agents["atlas"], ToolCall("1", "assemble_team",
                                                  {"agents": [{"id": "atlas", "name": "x", "role": "r", "system_prompt": "s"}]}), 0)
        assert "ERROR" in out

    def test_hermes_engine_falls_back_without_instance(self, tmp_path):
        o = _orch(tmp_path)
        o._tool(o.agents["atlas"], ToolCall("1", "assemble_team", {"agents": [
            {"id": "scout", "name": "Scout", "role": "r", "system_prompt": "s", "engine": "hermes_agent"}]}), 0)
        assert o.agents["scout"].get("engine") != "hermes_agent"
        assert "engine_note" in o.agents["scout"]


class TestTextProtocol:
    def test_parses_single_block(self):
        calls = Orchestrator.parse_text_calls('I will delegate now.\n<atlas>{"tool": "delegate", "args": {"agent_id": "x", "task": "t"}}</atlas>')
        assert len(calls) == 1 and calls[0].name == "delegate" and calls[0].args["agent_id"] == "x"

    def test_parses_multiple_and_trailing_comma(self):
        text = ('<atlas>{"tool": "list_agents", "args": {}}</atlas> then '
                '<atlas>{"tool": "finish", "args": {"summary": "done",}}</atlas>')
        calls = Orchestrator.parse_text_calls(text)
        assert [c.name for c in calls] == ["list_agents", "finish"]

    def test_ignores_garbage(self):
        assert Orchestrator.parse_text_calls("no blocks here") == []
        assert Orchestrator.parse_text_calls("<atlas>not json</atlas>") == []

    def test_prompt_lists_tools(self, tmp_path):
        o = _orch(tmp_path)
        p = o._text_protocol_prompt(o.agents["atlas"], ["delegate", "finish"])
        assert "<atlas>" in p and "delegate" in p


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestVideo:
    @pytest.fixture()
    def clip(self, tmp_path):
        f = tmp_path / "clip.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc=duration=4:size=320x240:rate=10", str(f)], check=True, timeout=60)
        return f

    def test_sample_video_frames(self, clip):
        frames, dur = vision.sample_video_frames(str(clip), 4)
        assert 3.5 < dur < 4.5
        assert 2 <= len(frames) <= 4
        assert all(j[:2] == b"\xff\xd8" for _t, j in frames)
        assert frames[0][0] < frames[-1][0]

    def test_describe_video_mocked_vlm(self, clip, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen["images"] = sum(1 for m in body["messages"] for p in (m["content"] if isinstance(m["content"], list) else [])
                                 if isinstance(p, dict) and p.get("type") == "image_url")
            return httpx.Response(200, json={"choices": [{"message": {"content": "A colour test pattern; counters increment."}}]})

        res = vision.describe_video(str(clip), "what is this?", frames=3,
                                    transport=httpx.MockTransport(handler))
        assert "test pattern" in res["answer"]
        assert seen["images"] >= 2
        assert res["frames"] and res["duration_s"] > 3

    def test_video_describe_tool_missing_file(self, tmp_path):
        o = _orch(tmp_path)
        out = o._tool(o.agents["atlas"], ToolCall("1", "video_describe", {"source": "nope.mp4"}), 0)
        assert "not found" in out
