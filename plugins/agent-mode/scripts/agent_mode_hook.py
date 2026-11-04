# -*- coding: utf-8 -*-
"""Agent Mode enforcement hook (PreToolUse + UserPromptSubmit).

Ports the DeepSeek Harness (DSH) agent-preset mode semantics to ZCode:

- PTC mode: mirrors the PTC executor's dispatch rejection. Under DSH's `ptc`
  selection a model-direct call naming any other visible tool resolves to
  UNKNOWN_TOOL before policy with the message reproduced verbatim below.
- Minimal mode: mirrors the minimal preset's tool roster, which mounts only a
  persistent shell plus `read_image`; every other tool is absent for the model.

State file: `~/.zcode/agent-mode.json` (user scope, wins) or
`.zcode/agent-mode.json` in the current workspace. Format:
`{"mode": "ptc" | "minimal"}` — anything else disables Agent Mode.

Exit codes: 0 pass, 2 deny (PreToolUse). JSON on stdout only for
UserPromptSubmit context injection.
"""
from __future__ import annotations

import io
import json
import os
import sys

# DSH read_image accepted extensions (packages/fs/tool-fs/src/read-image.ts).
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# DSH PTC executor rejection (packages/core/tool-executor-ptc/src/index.ts,
# ToolNotFoundError message under the `ptc` selection).
PTC_DENY = "only `run_code` is callable directly — call `{name}` from inside a `run_code` program instead"

# Zcode-side name of the reserved run_code transport provided by this plugin.
RUN_CODE_TOOL = "mcp__agent-mode__run_code"

PTC_CONTEXT = (
    "[Agent Mode: PTC] `run_code` is the only tool you can call directly — a tool "
    "call naming any other tool fails. The harness exposes it as "
    f"`{RUN_CODE_TOOL}`. Reach every tool the SDK declares in the agent-mode:ptc-mode "
    "skill from inside the program as `await tools.name(args)`; only printed values "
    "and the returned value become program output."
)

MINIMAL_CONTEXT = (
    "[Agent Mode: minimal] You are a helpful software engineer assistant. The minimal "
    "preset exposes only the persistent shell plus image reading: use the shell (Bash) "
    "for every file read, search, and mutation. Read, Write, Edit and the other file "
    "tools are denied; Read is allowed only on image files (read_image equivalent)."
)

MINIMAL_DENY = (
    "tool `{name}` is not part of the minimal preset — minimal mode exposes only the "
    "persistent shell (Bash) and image reading (Read on image files). Do the work "
    "through the shell instead."
)


def _candidate_state_files() -> list[str]:
    candidates: list[str] = []
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates.append(os.path.join(home, ".zcode", "agent-mode.json"))
    candidates.append(os.path.join(os.getcwd(), ".zcode", "agent-mode.json"))
    override = os.environ.get("AGENT_MODE_STATE_FILE")
    if override:
        candidates.insert(0, override)
    return candidates


def read_mode() -> str | None:
    """Resolve the active Agent Mode: 'ptc', 'minimal', or None."""
    for path in _candidate_state_files():
        try:
            with io.open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        mode = data.get("mode") if isinstance(data, dict) else None
        if mode in ("ptc", "minimal"):
            return str(mode)
        return None
    return None


def deny(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    raise SystemExit(2)


def is_image_read(tool_input: object) -> bool:
    if not isinstance(tool_input, dict):
        return False
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        return False
    lowered = file_path.lower()
    return lowered.endswith(IMAGE_EXTENSIONS)


def is_state_file_path(tool_input: object) -> bool:
    """True when the tool targets an Agent Mode state file.

    The mode switch itself must stay reachable from inside any mode, so
    Read/Write/Edit on the state file are exempt from mode enforcement.
    """
    if not isinstance(tool_input, dict):
        return False
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        return False
    normalized = file_path.replace("\\", "/").lower()
    return normalized.endswith("/.zcode/agent-mode.json") or normalized == ".zcode/agent-mode.json"


def handle_pre_tool_use() -> None:
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    # Fail open when the harness did not tell us which tool is calling, so a
    # payload-shape change can never brick the session; enforcement is exact
    # whenever tool_name is present.
    if not isinstance(tool_name, str) or tool_name == "":
        return

    mode = read_mode()
    if mode == "ptc":
        if tool_name != RUN_CODE_TOOL:
            if tool_name in ("Read", "Write", "Edit") and is_state_file_path(tool_input):
                return
            deny(PTC_DENY.format(name=tool_name))
        return
    if mode == "minimal":
        if tool_name == "Bash":
            return
        if tool_name == "Read" and (is_image_read(tool_input) or is_state_file_path(tool_input)):
            return
        if tool_name in ("Write", "Edit") and is_state_file_path(tool_input):
            return
        deny(MINIMAL_DENY.format(name=tool_name))
    return


def handle_user_prompt_submit() -> None:
    mode = read_mode()
    if mode == "ptc":
        context = PTC_CONTEXT
    elif mode == "minimal":
        context = MINIMAL_CONTEXT
    else:
        return
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "pre-tool-use":
        handle_pre_tool_use()
    elif command == "user-prompt-submit":
        handle_user_prompt_submit()
    # Unknown command: pass silently.
    raise SystemExit(0)


if __name__ == "__main__":
    main()
