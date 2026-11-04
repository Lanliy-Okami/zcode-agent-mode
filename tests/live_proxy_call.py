# -*- coding: utf-8 -*-
"""One-off live test: call a real proxied Github MCP tool through run_code (get_me, read-only)."""
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "plugins", "agent-mode", "mcp", "run_code_server.py")
WORKSPACE = tempfile.mkdtemp(prefix="agent-mode-live-")
STATE = os.path.join(WORKSPACE, "state.json")
with io.open(STATE, "w", encoding="utf-8") as handle:
    json.dump({"mode": "ptc"}, handle)

env = dict(os.environ)
env["AGENT_MODE_STATE_FILE"] = STATE

code = (
    "import json\n"
    "value = await tools.get_me({})\n"
    "text = value.get('text', '')\n"
    "structured = value.get('structured')\n"
    "login = None\n"
    "for candidate in (structured, text):\n"
    "    if isinstance(candidate, dict):\n"
    "        login = candidate.get('login') or (candidate.get('me') or {}).get('login') or login\n"
    "        break\n"
    "    if isinstance(candidate, str) and '\"login\"' in candidate:\n"
    "        try:\n"
    "            start = candidate.index('\"login\"')\n"
    "            login = candidate[start:start + 60]\n"
    "        except ValueError:\n"
    "            pass\n"
    "        break\n"
    "print('keys:', list(value.keys()))\n"
    "return {'login': login, 'textHead': text[:200]}\n"
)
payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
    "name": "run_code", "arguments": {"code": code, "description": "Live proxied get_me call"},
}}
process = subprocess.Popen(
    [sys.executable, "-X", "utf8", "-I", SERVER],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    cwd=WORKSPACE, env=env,
)
out, err = process.communicate(input=json.dumps(payload).encode("utf-8") + b"\n", timeout=180)
response = json.loads(out.decode("utf-8"))
result = response.get("result", {})
text = "\n".join(b["text"] for b in result.get("content", []) if b.get("type") == "text")
print("isError:", result.get("isError"))
print(text[:600])
if err:
    print("SERVER STDERR (tail):", err.decode("utf-8", "replace")[-600:])
