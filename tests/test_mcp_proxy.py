# -*- coding: utf-8 -*-
"""Proxy tests: a fake stdio MCP server is projected into run_code bindings."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "plugins", "agent-mode", "mcp", "run_code_server.py")
FAKE = os.path.join(HERE, "fake_mcp_server.py")

WORKSPACE = tempfile.mkdtemp(prefix="agent-mode-proxy-")
STATE_FILE = os.path.join(WORKSPACE, "agent-mode.json")
MCP_CONFIG = os.path.join(WORKSPACE, "mcp-config.json")
CACHE = os.path.join(os.path.expanduser("~"), ".zcode", "agent-mode-mcp-tools.json")

with io.open(STATE_FILE, "w", encoding="utf-8") as handle:
    json.dump({"mode": "ptc"}, handle)

fake_command = [sys.executable, "-X", "utf8", "-I", FAKE]
with io.open(MCP_CONFIG, "w", encoding="utf-8") as handle:
    json.dump({"mcp": {"servers": {"fake": {"type": "stdio", "command": fake_command[0], "args": fake_command[1:]}}}}, handle)


class Client:
    def __init__(self) -> None:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["AGENT_MODE_STATE_FILE"] = STATE_FILE
        env["AGENT_MODE_MCP_CONFIG"] = MCP_CONFIG
        env["AGENT_MODE_PROXY_CACHE"] = CACHE
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-I", SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKSPACE,
            env=env,
        )

    def call_run_code(self, code: str, description: str) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "run_code", "arguments": {"code": code, "description": description},
        }}
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        response = json.loads(self.process.stdout.readline().decode("utf-8"))
        return response["result"]

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.process.wait(timeout=10)


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def text_of(result: dict) -> str:
    return "\n".join(block["text"] for block in result.get("content", []) if block.get("type") == "text")


client = Client()

# 1. Proxied tool callable inside a program; result text flows back.
result = client.call_run_code(
    "value = await tools.echo({\"message\": \"hi from ptc\"})\nreturn {\"text\": value[\"text\"]}\n",
    "Call the proxied echo tool",
)
text = text_of(result)
check("proxied echo binding works", '"text": "echo: hi from ptc"' in text and result.get("isError") is not True, text[:300])

# 2. Proxied tools are fail-closed exclusive: two proxy calls never overlap.
result = client.call_run_code(
    "import asyncio\n"
    "values = await asyncio.gather(tools.echo({\"message\": \"a\"}), tools.echo({\"message\": \"b\"}))\n"
    "return {\"joined\": values[0][\"text\"] + values[1][\"text\"]}\n",
    "Gather two exclusive proxied calls",
)
text = text_of(result)
check("gather over exclusive proxies completes", '"joined": "echo: aecho: b"' in text.replace(", ", "") or "echo: a" in text, text[:300])

# 3. Unknown binding error names the registry scope.
result = client.call_run_code(
    "try:\n    await tools.does_not_exist({})\nexcept ToolCallError as error:\n    return {\"msg\": str(error)}\n",
    "Unknown binding error",
)
text = text_of(result)
check("unknown binding message", "unknown tool binding: does_not_exist" in text, text[:300])

# 4. Built-in binding still available alongside proxies.
result = client.call_run_code(
    "value = await tools.write({\"file_path\": \"built-in.txt\", \"content\": \"ok\"})\nreturn {\"op\": value[\"operation\"]}\n",
    "Write via built-in binding",
)
text = text_of(result)
check("built-in write still works", '"op": "create"' in text, text[:300])

client.close()

# 5. Discovery cache written for the hook.
try:
    with io.open(CACHE, "r", encoding="utf-8") as handle:
        cache = json.load(handle)
    names = [t["name"] for t in cache.get("tools", [])]
    check("cache lists the fake echo tool", "echo" in names, str(names)[:200])
    notes = cache.get("notes", [])
    fake_note = next((n for n in notes if n.get("server") == "fake"), None)
    check("cache notes fake server connected", fake_note is not None and "connected" in fake_note["message"], str(notes)[:300])
except FileNotFoundError:
    check("cache written", False, f"{CACHE} missing")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("ALL PROXY CHECKS PASSED")
