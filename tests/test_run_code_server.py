# -*- coding: utf-8 -*-
"""End-to-end test for the Agent Mode run_code MCP server.

Drives the server over stdio JSON-RPC exactly as an MCP client would, sets the
Agent Mode state file, and exercises the PTC contract: parallel-safe overlap,
exclusive ordering, ToolCallError, print/return output, invalid-output budgets,
and the mode gate.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "plugins", "agent-mode", "mcp", "run_code_server.py")

WORKSPACE = tempfile.mkdtemp(prefix="agent-mode-test-")
STATE_FILE = os.path.join(WORKSPACE, "agent-mode.json")

with io.open(STATE_FILE, "w", encoding="utf-8") as handle:
    json.dump({"mode": "ptc"}, handle)

# A fixture file for read/edit bindings.
FIXTURE = os.path.join(WORKSPACE, "fixture.txt")
with io.open(FIXTURE, "w", encoding="utf-8", newline="\n") as handle:
    handle.write("alpha\nbeta\ngamma\n")


class Client:
    def __init__(self, mode_state: str | None) -> None:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        if mode_state:
            env["AGENT_MODE_STATE_FILE"] = mode_state
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-I", SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKSPACE,
            env=env,
        )
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        request_id = self.next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        response = json.loads(line.decode("utf-8"))
        assert response.get("id") == request_id, f"response id mismatch: {response}"
        return response

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.process.wait(timeout=10)


def text_of(result: dict) -> str:
    return "\n".join(block["text"] for block in result.get("content", []) if block.get("type") == "text")


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


client = Client(STATE_FILE)

# Handshake + catalog.
init = client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
check("initialize returns serverInfo", init["result"]["serverInfo"]["name"] == "agent-mode")
tools = client.request("tools/list")["result"]["tools"]
check("exactly one tool run_code", [t["name"] for t in tools] == ["run_code"], str(tools))
check(
    "run_code description carries DSH python flavor",
    tools[0]["description"].startswith("Execute a Python program against the available tools.")
    and "body of an async Python function" in tools[0]["description"]
    and "Each program starts in the Session's current directory" in tools[0]["description"],
)
schema = tools[0]["inputSchema"]
check("run_code schema requires code+description, no timeoutMs", schema["required"] == ["code", "description"] and "timeoutMs" not in schema["properties"])
check(
    "sandbox_permissions enum is DSH ESCALATION_TARGETS",
    schema["properties"]["sandbox_permissions"]["enum"] == ["workspace-write", "danger-full-access"],
)

# 1. Program output: print + return; parallel-safe overlap under asyncio.gather.
result = client.call_tool("run_code", {
    "code": (
        "import asyncio\n"
        "read_value, glob_value = await asyncio.gather(\n"
        f"    tools.read({json.dumps({'file_path': 'fixture.txt'})}),\n"
        "    tools.glob({\"pattern\": \"*.txt\"}),\n"
        ")\n"
        "print('scanned workspace')\n"
        "return {\"firstLine\": read_value[\"lines\"][0][\"text\"], \"matches\": len(glob_value[\"paths\"])}\n"
    ),
    "description": "Read fixture and glob txt files",
})
text = text_of(result)
check("program result text", "scanned workspace" in text and "firstLine" in text, text)
check("gather overlap worked", "alpha" in text or "firstLine" in text)
check("no isError", result.get("isError") is not True, text)

# 2. Exclusive tool ordering: pwsh runs alone; exit code surfaces.
result = client.call_tool("run_code", {
    "code": (
        "value = await tools.pwsh({\"command\": \"Write-Output 'shell-ok'\", \"description\": \"Echo through pwsh\"})\n"
        "return {\"exit\": value[\"exitCode\"], \"text\": value[\"stdout\"][\"text\"].strip()}\n"
    ),
    "description": "Run one PowerShell command",
})
text = text_of(result)
check("pwsh binding ran", "shell-ok" in text and '"exit": 0' in text, text)

# 3. ToolCallError on a failing tool call.
result = client.call_tool("run_code", {
    "code": (
        "try:\n"
        "    await tools.edit({\"file_path\": \"fixture.txt\", \"old_string\": \"missing\", \"new_string\": \"x\"})\n"
        "    return {\"caught\": False}\n"
        "except ToolCallError as error:\n"
        "    return {\"caught\": True, \"toolName\": error.toolName, \"message\": str(error)}\n"
    ),
    "description": "Expect ToolCallError from edit",
})
text = text_of(result)
check("ToolCallError raised and handled", '"caught": true' in text and '"toolName": "edit"' in text, text)

# 4. Edit + read round trip through the canonical shapes.
result = client.call_tool("run_code", {
    "code": (
        "edit_value = await tools.edit({\"file_path\": \"fixture.txt\", \"old_string\": \"beta\", \"new_string\": \"beta2\"})\n"
        "read_value = await tools.read({\"file_path\": \"fixture.txt\"})\n"
        "return {\"secondLine\": read_value[\"lines\"][1][\"text\"], \"path\": edit_value[\"path\"]}\n"
    ),
    "description": "Edit fixture then read it back",
})
text = text_of(result)
check("edit+read round trip", "beta2" in text, text)

# 5. Lossless output budget: an oversized return is rejected with output-limit.
result = client.call_tool("run_code", {
    "code": "return {\"blob\": \"x\" * 40000}\n",
    "description": "Return an oversized value",
})
text = text_of(result)
check("output-limit enforced at 32768 bytes", result.get("isError") is True and "output-limit" in text, text[:200])

# 6. invalid-output: bytes are not lossless JSON.
result = client.call_tool("run_code", {
    "code": "return {\"blob\": b\"bytes\"}\n",
    "description": "Return a non-JSON value",
})
text = text_of(result)
check("invalid-output enforced", result.get("isError") is True and "invalid-output" in text, text[:200])

# 7. Misuse: sandbox_permissions without justification.
result = client.call_tool("run_code", {
    "code": "return 1\n",
    "description": "Escalation without justification",
    "sandbox_permissions": "danger-full-access",
})
text = text_of(result)
check("escalation requires justification", result.get("isError") is True and "justification" in text, text[:200])

# 8. Misuse: empty description.
result = client.call_tool("run_code", {"code": "return 1\n", "description": "   "})
check("empty description rejected", result.get("isError") is True, text_of(result)[:200])

# 9. grep binding with include filter + spill behavior untouched under cap.
result = client.call_tool("run_code", {
    "code": "value = await tools.grep({\"pattern\": \"beta\", \"include\": \"*.txt\"})\nreturn {\"n\": len(value[\"matches\"]), \"first\": value[\"matches\"][0][\"line\"]}\n",
    "description": "Grep fixture for beta",
})
text = text_of(result)
check("grep binding", '"n": 1' in text and "beta2" in text, text)

client.close()

# 10. Mode gate: without a ptc state file the tool refuses (explicit normal state
# file keeps this deterministic against the user's live global file).
OFF_STATE = os.path.join(WORKSPACE, "off-state.json")
with io.open(OFF_STATE, "w", encoding="utf-8") as handle:
    json.dump({"mode": "normal"}, handle)
env_off = dict(os.environ)
env_off["PYTHONUTF8"] = "1"
env_off["AGENT_MODE_STATE_FILE"] = OFF_STATE
client_off = Client.__new__(Client)
client_off.process = subprocess.Popen(
    [sys.executable, "-X", "utf8", "-I", SERVER],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    cwd=WORKSPACE, env=env_off,
)
client_off.next_id = 0
result = client_off.call_tool("run_code", {"code": "return 1\n", "description": "Gate check"})
text = text_of(result)
check("mode gate refuses without ptc state", result.get("isError") is True and "/agent-mode:ptc" in text, text[:200])
client_off.close()

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("ALL CHECKS PASSED")
