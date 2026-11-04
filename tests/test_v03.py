# -*- coding: utf-8 -*-
"""v0.3.0 feature tests: jobs, skill binding, ask_user_question (pending + elicitation), todo_write, per-session state."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "plugins", "agent-mode", "mcp", "run_code_server.py")

WORKSPACE = tempfile.mkdtemp(prefix="agent-mode-v03-")
STATE = os.path.join(WORKSPACE, "state.json")
with io.open(STATE, "w", encoding="utf-8") as handle:
    json.dump({"mode": "ptc"}, handle)

SKILL_DIR = os.path.join(WORKSPACE, ".zcode", "skills", "demo-skill")
os.makedirs(SKILL_DIR, exist_ok=True)
with io.open(os.path.join(SKILL_DIR, "SKILL.md"), "w", encoding="utf-8", newline="\n") as handle:
    handle.write("---\nname: demo-skill\ndescription: demo\n---\n\nBODY CONTENT HERE\n")

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def text_of(result: dict) -> str:
    return "\n".join(block["text"] for block in result.get("content", []) if block.get("type") == "text")


class Client:
    """Minimal MCP test client; optionally answers elicitation requests."""

    def __init__(self, elicitation: bool) -> None:
        self.elicitation = elicitation
        self.elicitation_lock = threading.Lock()
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["AGENT_MODE_STATE_FILE"] = STATE
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-I", SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=WORKSPACE, env=env,
        )
        self.next_id = 0
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        caps = {"tools": {}}
        if elicitation:
            caps["elicitation"] = {}
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": caps, "clientInfo": {"name": "t", "version": "0"}})

    def _read_loop(self) -> None:
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if message.get("method") == "elicitation/create":
                request_id = message.get("id")
                self._write({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"action": "accept", "content": {"choice": "Option A"}},
                })
                continue
            with self.elicitation_lock:
                self._responses[message.get("id")] = message
                self._event.set()

    def _write(self, payload: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.time() + 120
        while time.time() < deadline:
            with self.elicitation_lock:
                message = self._responses.pop(request_id, None)
            if message is not None:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message"))
                return message.get("result") or {}
            time.sleep(0.05)
        raise RuntimeError("client request timeout")

    _responses: dict = {}
    _event = threading.Event()

    def run_program(self, code: str, description: str) -> dict:
        result = self.request("tools/call", {"name": "run_code", "arguments": {"code": code, "description": description}})
        return result

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.process.wait(timeout=15)


client = Client(elicitation=False)

# 1. Background pwsh job + job_output + job_kill.
result = client.run_program(
    "value = await tools.pwsh({\"command\": \"1..3 | ForEach-Object { Write-Output \\\"line $_\\\"; Start-Sleep -Milliseconds 200 }\", \"description\": \"Slow background echo\", \"run_in_background\": True})\n"
    "return {\"kind\": value[\"kind\"], \"jobId\": value[\"jobId\"]}\n",
    "Start a background job",
)
text = text_of(result)
check("background job started", '"kind": "background"' in text, text[:300])

import re
job_id_match = re.search(r'"jobId": "(job-[^"]+)"', text)
check("job id returned", job_id_match is not None, text[:300])
if job_id_match:
    job_id = job_id_match.group(1)
    result = client.run_program(
        f"value = await tools.job_output({{\"job_id\": \"{job_id}\", \"wait\": True, \"timeout_ms\": 5000}})\n"
        "return {\"status\": value[\"job\"][\"status\"], \"hasLines\": (\"line 1\" in value[\"text\"])}\n",
        "Wait for the background job",
    )
    text = text_of(result)
    check("job_output collected output", '"hasLines": true' in text and '"status": "completed"' in text, text[:400])

    result = client.run_program(
        f"value = await tools.job_list({{}})\nreturn {{\"n\": len(value), \"kind\": value[0][\"kind\"] if value else None}}\n",
        "List jobs",
    )
    text = text_of(result)
    check("job_list works", '"n": 1' in text and '"kind": "pwsh"' in text, text[:300])

    result = client.run_program(
        f"value = await tools.job_output({{\"job_id\": \"{job_id}\"}})\n"
        "try:\n"
        f"    value = await tools.job_kill({{\"job_id\": \"{job_id}\"}})\n"
        "    outcome = value[\"outcome\"]\n"
        "except ToolCallError as error:\n"
        "    outcome = str(error)\n"
        "return {\"outcome\": outcome}\n",
        "Kill the finished job",
    )
    text = text_of(result)
    check("job_kill already-finished", '"outcome": "already-finished"' in text, text[:300])

# 2. promoteOnTimeout: a killed-on-timeout command becomes a promoted job.
result = client.run_program(
    "value = await tools.pwsh({\"command\": \"Start-Sleep -Seconds 30; Write-Output done\", \"description\": \"Long sleep\", \"timeoutMs\": 800})\n"
    "return {\"kind\": value[\"kind\"], \"jobId\": value[\"jobId\"]}\n",
    "Timeout promotes to background",
)
text = text_of(result)
check("timeout promotes to background job", '"kind": "promoted"' in text and '"jobId": "job-' in text, text[:300])
if '"jobId": "job-' in text:
    promoted_id = re.search(r'"jobId": "(job-[^"]+)"', text).group(1)
    result = client.run_program(
        f"value = await tools.job_kill({{\"job_id\": \"{promoted_id}\", \"reason\": \"test cleanup\"}})\n"
        "return {\"outcome\": value[\"outcome\"], \"status\": value[\"job\"][\"status\"]}\n",
        "Kill the promoted job",
    )
    text = text_of(result)
    check("promoted job killed", '"outcome": "cancellation-requested"' in text and '"status": "killed"' in text, text[:300])

# 3. skill binding loads SKILL.md body (frontmatter stripped).
result = client.run_program(
    "value = await tools.skill({\"name\": \"demo-skill\"})\nreturn {\"content\": value[\"content\"]}\n",
    "Load a skill via binding",
)
text = text_of(result)
check("skill binding returns body", "BODY CONTENT HERE" in text and "description: demo" not in text, text[:300])

result = client.run_program(
    "try:\n    await tools.skill({\"name\": \"nope\"})\nexcept ToolCallError as error:\n    return {\"msg\": str(error)}\n",
    "Unknown skill error",
)
text = text_of(result)
check("unknown skill message", 'is unknown or no longer available' in text, text[:300])

# 4. todo_write contract.
result = client.run_program(
    "value = await tools.todo_write({\"todos\": [{\"content\": \"step one\", \"status\": \"completed\"}, {\"content\": \"step two\", \"status\": \"in_progress\"}]})\n"
    "return {\"counts\": value[\"counts\"]}\n",
    "Write todos",
)
text = text_of(result)
check("todo_write counts", '"inProgress": 1' in text and '"completed": 1' in text, text[:300])

# 5. ask_user_question without elicitation support → DSH timed pending shape.
result = client.run_program(
    "value = await tools.ask_user_question({\"questions\": [{\"id\": \"choice\", \"question\": \"A or B?\"}]})\n"
    "return {\"pending\": value.get(\"pending\"), \"callId\": value.get(\"callId\") is not None}\n",
    "Ask without elicitation support",
)
text = text_of(result)
check("pending fallback shape", '"pending": true' in text and '"callId": true' in text, text[:300])

# 6. Multi-line string literals survive BYTE-EXACT through run_code (regression:
# the v0.3.0 textual-indent bootstrap corrupted triple-quoted YAML — every
# continuation line gained 4 spaces, producing invalid workflow files).
result = client.run_program(
    'yaml = \"\"\"on:\n  push:\n    branches: [main]\"\"\"\n'
    "value = await tools.write({\"file_path\": \"wf.yml\", \"content\": yaml})\n"
    "readback = await tools.read({\"file_path\": \"wf.yml\"})\n"
    "return {\"after\": value[\"after\"], \"lines\": [line[\"text\"] for line in readback[\"lines\"]]}\n",
    "Write multi-line YAML through run_code",
)
text = text_of(result)
check(
    "multi-line string byte-exact (AST splice)",
    '"after": "on:\\n  push:\\n    branches: [main]"' in text,
    text[:400],
)
check("yaml lines exact, no phantom indent", '"    branches: [main]"' in text and '"        branches:' not in text, text[:400])

# 7. Fully timeout-free defaults: a slow foreground pwsh completes without any
# timeoutMs (the 0 = unlimited configuration), and no wall deadline interferes.
result = client.run_program(
    "value = await tools.pwsh({\"command\": \"Start-Sleep -Seconds 3; Write-Output late-ok\", \"description\": \"Slow foreground\"})\n"
    "return {\"text\": value[\"stdout\"][\"text\"].strip(), \"timedOut\": value[\"timedOut\"], \"kind\": value[\"kind\"]}\n",
    "Slow foreground under timeout-free config",
)
text = text_of(result)
check("timeout-free foreground pwsh completes", "late-ok" in text and '"timedOut": false' in text and '"kind": "foreground"' in text, text[:300])

client.close()

# 6. WITH elicitation support: blocking ask returns DSH answers.
client_elicit = Client(elicitation=True)
result = client_elicit.run_program(
    "value = await tools.ask_user_question({\"questions\": [{\"id\": \"choice\", \"question\": \"A or B?\", \"options\": [{\"label\": \"Option A\"}, {\"label\": \"Option B\"}]}]})\n"
    "return {\"answers\": value[\"answers\"]}\n",
    "Ask with elicitation support",
)
text = text_of(result)
check("elicitation blocking answer", '"selected": [' in text and '"Option A"' in text, text[:400])
client_elicit.close()

# 7. Per-session mode state isolation (hook-level).
home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
SESSIONS_DIR = os.path.join(home, ".zcode", "agent-mode-sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)
SID_A = "sess-test-a"
SID_B = "sess-test-b"
for sid in (SID_A, SID_B):
    try:
        os.remove(os.path.join(SESSIONS_DIR, f"{sid}.json"))
    except OSError:
        pass

GLOBAL_STATE = os.path.join(home, ".zcode", "agent-mode.json")
saved_global = None
try:
    with io.open(GLOBAL_STATE, "r", encoding="utf-8") as handle:
        saved_global = handle.read()
except OSError:
    pass

def run_hook(mode_env: dict | None, sid: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.pop("AGENT_MODE_STATE_FILE", None)
    if mode_env is not None:
        with io.open(GLOBAL_STATE, "w", encoding="utf-8") as handle:
            json.dump(mode_env, handle)
    if sid:
        env["CLAUDE_SESSION_ID"] = sid
    else:
        env.pop("CLAUDE_SESSION_ID", None)
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-I", os.path.join(HERE, "..", "plugins", "agent-mode", "scripts", "agent_mode_hook.py"), "pre-tool-use"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {}}).encode("utf-8"),
        capture_output=True, env=env,
    )

# A activates PTC (as the command does: with the session's owner stamp)
# → A enforced, B unaffected.
proc_a = run_hook({"mode": "ptc", "owner": SID_A}, SID_A)
check("session A adopts ptc", proc_a.returncode == 2, proc_a.stderr.decode("utf-8"))
proc_b = run_hook(None, SID_B)
check("session B unaffected by A's switch", proc_b.returncode == 0, proc_b.stderr.decode("utf-8"))
# B switches to normal → A keeps ptc, B free.
proc_b2 = run_hook({"mode": "normal", "owner": SID_B}, SID_B)
check("session B clears its mode", proc_b2.returncode == 0, proc_b2.stderr.decode("utf-8"))
proc_a2 = run_hook(None, SID_A)
check("session A keeps ptc after B cleared", proc_a2.returncode == 2, proc_a2.stderr.decode("utf-8"))
# Restore.
for sid in (SID_A, SID_B):
    try:
        os.remove(os.path.join(SESSIONS_DIR, f"{sid}.json"))
    except OSError:
        pass

if saved_global is not None:
    with io.open(GLOBAL_STATE, "w", encoding="utf-8") as handle:
        handle.write(saved_global)
else:
    try:
        os.remove(GLOBAL_STATE)
    except OSError:
        pass

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("ALL v0.3.0 CHECKS PASSED")
