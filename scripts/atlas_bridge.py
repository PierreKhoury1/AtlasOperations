#!/usr/bin/env python3
"""Atlas bridge: run your desk's agents on your own computer, on your own AI subscription.

The desk sends each model call to this runner; the runner answers it with the CLI you already have installed and
signed in (Claude Code, Codex, Gemini CLI, or any command) and streams the answer back. Your login stays here: the
desk only ever sees the text your agent writes. Standard library only.

    py atlas_bridge.py --url https://your-desk --token atb_... --agent claude
    py atlas_bridge.py --url ... --token ... --agent codex
    py atlas_bridge.py --url ... --token ... --agent gemini
    py atlas_bridge.py --url ... --token ... --agent cmd --cmd "ollama run llama3"      # prompt on stdin, answer on stdout
    py atlas_bridge.py --url ... --token ... --agent claude --parallel 4                 # up to 4 CLI processes at once

Get the token from the desk: Desk setup -> Your AI accounts -> Your agent -> Connect a computer.
The desk's tools (CRM, approvals, cameras, records) reach the agent through a text protocol in the prompt, so the
CLI runs with its own file/shell tools switched off: it is used as a model, nothing on this computer is touched.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

VERSION = "0.3"
CLAUDE_OFF = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite,MultiEdit,KillShell,BashOutput"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Desk:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body=None, timeout=40):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                                              "X-Atlas-Bridge": VERSION, "User-Agent": "atlas-bridge/" + VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")


def transcript(job: dict) -> str:
    """The conversation as one prompt: the system brief first, then every turn, then the ask."""
    out = []
    for m in job.get("messages") or []:
        role = "USER" if m.get("role") == "user" else "ASSISTANT"
        out.append(f"### {role}\n{m.get('content', '')}".rstrip())
    out.append("### ASSISTANT\n")
    return "\n\n".join(out)


def _which(name: str) -> str:
    p = shutil.which(name) or (shutil.which(name + ".cmd") if sys.platform == "win32" else None)
    if not p:
        raise RuntimeError(f"{name} is not installed or not on PATH")
    return p


def run_claude(job: dict, model: str, timeout: int) -> str:
    """claude -p: the system brief goes in a file (--system-prompt-file: a desk's brief runs to tens of KB and Windows
    caps a command line at 32K), the conversation on stdin. Every built-in tool is off: the desk's tools reach the
    agent through its text protocol, and nothing on this computer is read or run."""
    system = job.get("system") or ""
    base = [_which("claude"), "-p", "--output-format", "text", "--disallowedTools", CLAUDE_OFF]
    if model:
        base += ["--model", model]
    fd, sp = tempfile.mkstemp(suffix=".md", prefix="atlas-sys-")
    os.close(fd)
    try:
        with open(sp, "w", encoding="utf-8") as f:
            f.write(system)
        try:
            return _run(base + ["--system-prompt-file", sp], transcript(job), timeout)
        except RuntimeError as e:
            if "system-prompt-file" not in str(e):
                raise
            return _run(base + ["--system-prompt", system], transcript(job), timeout)   # an older claude: inline
    finally:
        try:
            os.unlink(sp)
        except OSError:
            pass


def run_codex(job: dict, model: str, timeout: int) -> str:
    fd, last = tempfile.mkstemp(suffix=".txt", prefix="atlas-codex-")
    os.close(fd)
    args = [_which("codex"), "exec", "--skip-git-repo-check", "-s", "read-only", "--output-last-message", last, "-"]
    if model:
        args += ["-m", model]
    prompt = (job.get("system") or "") + "\n\n" + transcript(job)
    try:
        out = _run(args, prompt, timeout)
        try:
            with open(last, encoding="utf-8") as f:
                final = f.read().strip()
            return final or out
        except OSError:
            return out
    finally:
        try:
            os.unlink(last)
        except OSError:
            pass


def run_gemini(job: dict, model: str, timeout: int) -> str:
    args = [_which("gemini"), "-p", "Answer the last ASSISTANT turn of the conversation given on stdin, following its system brief exactly. Write only the answer."]
    if model:
        args += ["-m", model]
    prompt = "SYSTEM BRIEF:\n" + (job.get("system") or "") + "\n\n" + transcript(job)
    return _run(args, prompt, timeout)


HOST_DIR = os.environ.get("ATLAS_HOST_DIR") or os.path.join(os.path.expanduser("~"), ".atlas-host")


def run_host(job: dict, model: str, timeout: int, on_line) -> str:
    """A hosted turn: one Claude Code session per desk, resumed every turn. Built-in tools off, only the desk's MCP
    tools (the job's mcp url + token). Every stream-json line goes back to the desk as it is printed; the desk may
    answer {"stop": true} (a usage limit) and the process is killed. Returns the final result text."""
    ws = os.path.join(HOST_DIR, str(job.get("desk_id") or "0"))
    os.makedirs(ws, exist_ok=True)
    sid = str(job.get("session_id") or "")
    mcp = job.get("mcp") or {}
    mcp_path = os.path.join(ws, "atlas-mcp.json")
    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {str(mcp.get("name") or "atlas"): {"type": "http", "url": mcp.get("url", ""),
                                                                      "headers": {"Authorization": "Bearer " + str(mcp.get("token") or "")}}}}, f)
    brief_path = os.path.join(ws, "ATLAS_BRIEF.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(job.get("system") or "")
    base = [_which("claude"), "-p", "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--mcp-config", mcp_path, "--tools", "", "--allowedTools", "mcp__" + str(mcp.get("name") or "atlas"),
            "--setting-sources", "", "--system-prompt-file", brief_path, "--max-turns", str(int(job.get("max_turns") or 60))]
    if model:
        base += ["--model", model]
    task = str(job.get("task") or "")

    def attempt(resume: bool):
        args = base + (["--resume", sid] if resume else ["--session-id", sid])
        cmd, shell = args, False
        if sys.platform == "win32" and str(cmd[0]).lower().endswith(".cmd"):
            cmd, shell = subprocess.list2cmdline(cmd), True
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", shell=shell, cwd=ws)
        result, stopped = "", False
        t_end = time.time() + timeout
        try:
            p.stdin.write(task)
            p.stdin.close()
            for line in p.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = {}
                if ev.get("type") == "result":
                    result = str(ev.get("result") or "")
                if on_line(line) is False:
                    stopped = True
                    p.kill()
                    break
                if time.time() > t_end:
                    p.kill()
                    raise subprocess.TimeoutExpired(cmd, timeout)
        finally:
            try:
                p.wait(timeout=30)
            except Exception:
                p.kill()
        err = (p.stderr.read() or "").strip() if p.stderr else ""
        return p.returncode, result, err, stopped

    code, result, err, stopped = attempt(bool(job.get("resume")) and bool(sid))
    if code != 0 and not stopped and job.get("resume") and ("No conversation found" in err or "session" in err.lower()):
        log(f"session {sid[:8]} not on this machine; starting it afresh")
        code, result, err, stopped = attempt(False)
    if code != 0 and not result and not stopped:
        raise RuntimeError(f"claude exited {code}: {err[-600:] or 'no output'}")
    return result


def run_cmd(cmd: str, job: dict, timeout: int) -> str:
    prompt = "SYSTEM:\n" + (job.get("system") or "") + "\n\n" + transcript(job)
    return _run(cmd, prompt, timeout, shell=True)


def _run(args, stdin: str, timeout: int, shell: bool = False) -> str:
    cmd = args if not shell else args
    if sys.platform == "win32" and not shell and str(cmd[0]).lower().endswith(".cmd"):
        cmd = subprocess.list2cmdline(cmd)                 # npm shims need the shell on Windows
        shell = True
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=shell)
    if p.returncode != 0 and not p.stdout.strip():
        raise RuntimeError((p.stderr or f"exit {p.returncode}").strip()[-800:])
    return p.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="the desk, e.g. https://2.28.138.218.sslip.io")
    ap.add_argument("--token", required=True, help="the connection token from Desk setup")
    ap.add_argument("--agent", default="claude", choices=["claude", "codex", "gemini", "cmd"])
    ap.add_argument("--cmd", default="", help="with --agent cmd: a command that reads the prompt on stdin and prints the answer")
    ap.add_argument("--model", default="", help="model name to pass to the CLI (optional)")
    ap.add_argument("--timeout", type=int, default=480, help="seconds per answer")
    ap.add_argument("--once", action="store_true", help="answer one job and exit (testing)")
    ap.add_argument("--parallel", type=int, default=1, help="CLI processes at once (a machine serving many desks: 2-8)")
    a = ap.parse_args()
    if a.agent == "cmd" and not a.cmd:
        ap.error("--agent cmd needs --cmd")
    if a.agent != "cmd":
        _which(a.agent)
    desk = Desk(a.url, a.token)
    n = max(1, min(16, int(a.parallel or 1)))
    log(f"atlas bridge {VERSION}: {a.agent} on this computer <- {a.url}" + (f" ({n} at once)" if n > 1 else ""))
    if n == 1 or a.once:
        return serve(desk, a, 0)
    stop = {"code": None}

    def worker(slot: int):
        code = serve(desk, a, slot)
        stop["code"] = code

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads) and stop["code"] is None:
        time.sleep(1)
    return int(stop["code"] or 0)


def serve(desk: "Desk", a, slot: int) -> int:
    """One poll loop: take a job, answer it with the CLI, repeat. `slot` names the loop in the log when several run."""
    tag = f"[{slot}] " if a.parallel and a.parallel > 1 else ""
    backoff = 2
    while True:
        try:
            r = desk.call("GET", "/api/bridge/jobs?wait=25", timeout=40)
            backoff = 2
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log(tag + "the desk rejected this token (removed in Desk setup?) - stopping")
                return 2
            log(f"{tag}desk answered {e.code}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(60, backoff * 2)
            continue
        except Exception as e:
            log(f"{tag}cannot reach the desk ({e}); retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(60, backoff * 2)
            continue
        job = (r or {}).get("job")
        if not job:
            continue
        jid = job["id"]
        log(f"{tag}job {jid}: {len(job.get('messages') or [])} turns -> {a.agent}")
        t0 = time.time()
        try:
            desk.call("POST", f"/api/bridge/jobs/{jid}/chunk", {"text": ""})       # "working": the desk shows the agent is on it
            if job.get("kind") == "host":
                if a.agent != "claude":
                    raise RuntimeError(f"hosted sessions need --agent claude (this runner is {a.agent})")

                def relay(line: str):
                    r = desk.call("POST", f"/api/bridge/jobs/{jid}/chunk", {"text": line}, timeout=60)
                    return not (r or {}).get("stop")
                text = run_host(job, a.model, max(a.timeout, int(job.get("timeout") or 1500)), relay)
            elif a.agent == "claude":
                text = run_claude(job, a.model, a.timeout)
            elif a.agent == "codex":
                text = run_codex(job, a.model, a.timeout)
            elif a.agent == "gemini":
                text = run_gemini(job, a.model, a.timeout)
            else:
                text = run_cmd(a.cmd, job, a.timeout)
            desk.call("POST", f"/api/bridge/jobs/{jid}", {"text": text, "seconds": round(time.time() - t0, 1)}, timeout=60)
            log(f"{tag}job {jid}: answered, {len(text)} chars in {time.time() - t0:.1f}s")
        except subprocess.TimeoutExpired:
            desk.call("POST", f"/api/bridge/jobs/{jid}", {"error": f"{a.agent} took longer than {a.timeout}s"}, timeout=60)
            log(f"{tag}job {jid}: timed out")
        except Exception as e:
            try:
                desk.call("POST", f"/api/bridge/jobs/{jid}", {"error": str(e)[:800]}, timeout=60)
            except Exception:
                pass
            log(f"{tag}job {jid}: failed: {e}")
        if a.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
