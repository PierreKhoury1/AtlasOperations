"""Tiny MCP server (stdio) used to verify the Hermes MCP client. Tools: echo, add, create_note.
Run: py workspace/inputs/demo_mcp_server.py   (Hermes starts it for you as a connector command)."""
import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Echo text back.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}},
    {"name": "create_note", "description": "Create a note in the notes system (a write).", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}}, "required": ["title"]}},
]
NOTES = []


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        m = json.loads(line)
    except Exception:
        continue
    method, id_, params = m.get("method"), m.get("id"), m.get("params") or {}
    if method == "initialize":
        reply(id_, {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                    "serverInfo": {"name": "demo-notes", "version": "0.1"}})
    elif method == "tools/list":
        reply(id_, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            reply(id_, {"content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}]})
        elif name == "add":
            reply(id_, {"content": [{"type": "text", "text": str(float(args.get("a", 0)) + float(args.get("b", 0)))}]})
        elif name == "create_note":
            NOTES.append({"title": args.get("title"), "body": args.get("body", ""), "at": time.time()})
            reply(id_, {"content": [{"type": "text", "text": f"note created: {args.get('title')} (total {len(NOTES)})"}]})
        else:
            reply(id_, error={"code": -32601, "message": f"unknown tool {name}"})
    elif id_ is not None:
        reply(id_, {})
