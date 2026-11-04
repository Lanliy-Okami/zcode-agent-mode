# -*- coding: utf-8 -*-
"""Enforcement hook tests: PTC-only denial, minimal whitelist, state-file exemption, context."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "agent-mode", "scripts", "agent_mode_hook.py")
WORKSPACE = tempfile.mkdtemp(prefix="agent-mode-hook-")
STATE = os.path.join(WORKSPACE, "agent-mode.json")

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def run_hook(event: str, payload: dict, mode: str | None) -> subprocess.CompletedProcess:
    if mode is None:
        try:
            os.remove(STATE)
        except OSError:
            pass
    else:
        with io.open(STATE, "w", encoding="utf-8") as handle:
            json.dump({"mode": mode}, handle)
    env = dict(os.environ)
    env["AGENT_MODE_STATE_FILE"] = STATE
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", HOOK, event],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        env=env,
    )
    return proc


# --- PTC mode ---
proc = run_hook("pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, "ptc")
check("PTC denies direct Bash", proc.returncode == 2)
check(
    "PTC deny message is DSH-verbatim",
    b"only `run_code` is callable directly" in proc.stderr and b"call `Bash` from inside a `run_code` program instead" in proc.stderr,
    proc.stderr.decode("utf-8"),
)

proc = run_hook("pre-tool-use", {"tool_name": "mcp__plugin_agent-mode_agent-mode__run_code", "tool_input": {}}, "ptc")
check("PTC allows plugin-prefixed run_code", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "mcp__agent-mode__run_code", "tool_input": {}}, "ptc")
check("PTC allows bare-prefixed run_code (back-compat)", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Skill", "tool_input": {"skill": "agent-mode:ptc-mode"}}, "ptc")
check("PTC exempts agent-mode Skill loads", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Skill", "tool_input": {"skill": "other-plugin:skill"}}, "ptc")
check("PTC denies unrelated Skill loads", proc.returncode == 2)

proc = run_hook("pre-tool-use", {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}, "ptc")
check("PTC exempts AskUserQuestion", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Agent", "tool_input": {"description": "research"}}, "ptc")
check("PTC exempts Agent delegation", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Task", "tool_input": {}}, "ptc")
check("PTC exempts Task alias", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "AskUserQuestion", "tool_input": {}}, "minimal")
check("minimal exempts AskUserQuestion", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Agent", "tool_input": {}}, "minimal")
check("minimal denies Agent delegation", proc.returncode == 2)

proc = run_hook("pre-tool-use", {"tool_name": "Write", "tool_input": {"file_path": r"C:\Users\x\.zcode\agent-mode.json", "content": "{}"}}, "ptc")
check("PTC exempts state-file Write", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Write", "tool_input": {"file_path": r"C:\Users\x\other.json", "content": "{}"}}, "ptc")
check("PTC denies unrelated Write", proc.returncode == 2)

# --- minimal mode ---
proc = run_hook("pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, "minimal")
check("minimal allows Bash", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Read", "tool_input": {"file_path": r"C:\x\photo.PNG"}}, "minimal")
check("minimal allows image Read", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Read", "tool_input": {"file_path": r"C:\x\main.ts"}}, "minimal")
check("minimal denies text Read", proc.returncode == 2)

proc = run_hook("pre-tool-use", {"tool_name": "Write", "tool_input": {"file_path": r"C:\x\main.ts"}}, "minimal")
check("minimal denies Write", proc.returncode == 2)

proc = run_hook("pre-tool-use", {"tool_name": "Grep", "tool_input": {}}, "minimal")
check("minimal denies Grep", proc.returncode == 2)

proc = run_hook("pre-tool-use", {"tool_name": "mcp__plugin_agent-mode_agent-mode__run_code", "tool_input": {}}, "minimal")
check("minimal denies run_code", proc.returncode == 2)

proc = run_hook("pre-tool-use", {"tool_name": "Skill", "tool_input": {"skill": "agent-mode:minimal-mode"}}, "minimal")
check("minimal exempts agent-mode Skill loads", proc.returncode == 0)

proc = run_hook("pre-tool-use", {"tool_name": "Read", "tool_input": {"file_path": r"C:\Users\demo\.zcode\agent-mode.json"}}, "minimal")
check("minimal exempts state-file Read", proc.returncode == 0)

# --- no mode ---
proc = run_hook("pre-tool-use", {"tool_name": "Bash", "tool_input": {}}, None)
check("off mode passes everything", proc.returncode == 0)

# --- UserPromptSubmit context ---
proc = run_hook("user-prompt-submit", {"prompt": "hi"}, "ptc")
out = proc.stdout.decode("utf-8")
check("PTC prompt context emitted", proc.returncode == 0 and "UserPromptSubmit" in out and "only tool you can call directly" in out, out[:200])

proc = run_hook("user-prompt-submit", {"prompt": "hi"}, "minimal")
out = proc.stdout.decode("utf-8")
check("minimal prompt context emitted", proc.returncode == 0 and "helpful software engineer assistant" in out, out[:200])

proc = run_hook("user-prompt-submit", {"prompt": "hi"}, None)
check("off mode emits nothing", proc.returncode == 0 and proc.stdout == b"")

# --- fail-open on malformed payload ---
proc = run_hook("pre-tool-use", {}, "ptc")
check("missing tool_name fails open", proc.returncode == 0)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("ALL HOOK CHECKS PASSED")
