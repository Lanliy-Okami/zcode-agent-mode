# -*- coding: utf-8 -*-
"""Assemble the two mode SKILL.md files from the generated SDK section."""
from __future__ import annotations

import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "plugins", "agent-mode")

sdk = io.open(os.path.join(HERE, "..", "plugins", "agent-mode", "mcp", "sdk_section.txt"), encoding="utf-8").read().strip()

PTC_SKILL = f"""---
name: ptc-mode
description: DeepSeek Harness (DSH) agent preset "ptc" mode for ZCode — programmatic tool calling. Load when the session is in PTC mode or the user asks for PTC / run_code / programmatic tool calling: every tool call must go through a run_code program using the generated Python SDK.
---

# PTC mode (DeepSeek Harness agent preset)

You are a coding agent powered by the current session's model.

`run_code` is the only tool you can call directly — a tool call naming any other tool fails. Reach every tool the SDK declares below from inside the program.

ZCode exposes the reserved transport as **`mcp__plugin_agent-mode_agent-mode__run_code`** (plugin-prefixed MCP naming). A direct call to any other tool is rejected before policy with: `only \\`run_code\\` is callable directly — call \\`<name>\\` from inside a \\`run_code\\` program instead`. Exceptions: `AskUserQuestion` (client-UI interaction) stays directly callable.

## Transport contract

- The program is the body of an async Python function. Top-level await and return are supported. Emit output with print(...) or return a lossless JSON value. Only printed values and the returned value become program output. Each call runs in a fresh Python process; variables and imported modules do not persist across calls. Relative paths use the supplied working directory; only TMPDIR is set in the environment. Direct filesystem access follows this execution's sandbox policy.
- Each program starts in the Session's current directory. Running programs keep their initial directory.
- A sandbox escalation approves this complete program for one execution only. Nested tools retain their own policies and approvals. Request wider access only after evidence of a denial. Earlier effects may already have completed: inspect them before explicitly retrying. Programs are never replayed automatically.
- This deployment runs FULLY TIMEOUT-FREE by default (DSH's budgets are configuration): the program wall deadline, `pwsh` timeoutMs default/cap, background-job waits, proxied MCP calls, `ask_user_question` blocking, and `web_fetch` sockets all wait indefinitely; `0 = unlimited` is the default of every `AGENT_MODE_*` budget variable (`AGENT_MODE_MAX_WALL_MS`, `AGENT_MODE_SHELL_DEFAULT_TIMEOUT_MS`, `AGENT_MODE_SHELL_MAX_TIMEOUT_MS`, `AGENT_MODE_JOB_WAIT_DEFAULT_MS`, `AGENT_MODE_JOB_WAIT_MAX_MS`, `AGENT_MODE_MCP_CALL_TIMEOUT_S`, `AGENT_MODE_ASK_TIMEOUT_S`, `AGENT_MODE_FETCH_TIMEOUT_S`). Set a variable to a positive millisecond/second value to re-enable that deadline; an expired `pwsh` timeoutMs still PROMOTES the command to a background job instead of killing it.
- `run_code` requires Agent Mode PTC mode (`{{"mode": "ptc"}}` in `%USERPROFILE%\\.zcode\\agent-mode.json`, set by `/agent-mode:ptc`). Without it the transport refuses.

{sdk}

## Registry notes

- The declared bindings are the model-facing surface: `pwsh`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch`, `ask_user_question`, `todo_write`, `job_output`, `job_list`, `job_kill`, `skill` — the DSH `ptc` preset's tool roster with DSH's verbatim descriptions and canonical output shapes. Safe (read-only) calls may overlap under `asyncio.gather`; mutating and user-blocking calls (`pwsh`, `write`, `edit`, `ask_user_question`, `todo_write`, `job_kill`) run alone, in submission order.
- **Background jobs** (`pwsh` with `run_in_background`, or a timeout that promotes the command instead of killing it): DSH `tool:jobs` policy applies verbatim — Track every background job id you start. You are notified in-session when a job finishes — do not busy-poll or sleep on one; keep working on independent steps and do not duplicate a running job's work. Before giving a final answer, collect every still-relevant job with job_output (set wait: true only when you are genuinely blocked on it), and job_kill jobs that stopped mattering.
- **`ask_user_question`** blocks inside the program when the client supports MCP elicitation (the answer batch becomes the binding value, DSH legacy contract); without elicitation support it returns DSH's timed-mode pending shape — ask the user directly afterwards, and their reply is the answer.
- **`skill`** loads a skill's full SKILL.md body by exact name (DSH tool-skill contract); the session's skill catalog comes from ZCode natively.
- **Mode state is per session** (like DSH session modes): switching writes `{{"mode": ..., "owner": "<session id>"}}` — the session id comes from the "Agent Mode session" banner in the injected context. Another session's switch never leaks into this one.
- **User interaction exception**: `AskUserQuestion` stays DIRECTLY callable in PTC mode (a deviation from DSH, where `ask_user_question` is an SDK binding). An MCP server cannot open ZCode's question dialog, so the client-UI tool is the only channel; all work still flows through `run_code`.
- **Projected MCP bindings**: like DSH, the registry binds every agent-visible tool — the tools of the MCP servers configured in ZCode (user/workspace scope) are projected as additional bindings and listed at the top of each session ("PTC projected MCP bindings"). Call them by bare name (`await tools.get_me({{}})`) or subscript; their arguments come lossless-JSON as plain dict/list (the projected list names each tool's required args). They classify FAIL-CLOSED EXCLUSIVE (DSH's rule for undeclared tools): they run alone, in submission order, never overlapping. A server that cannot be reached is skipped and noted in the same context block; servers whose commands carry unresolvable `${{...}}` plugin placeholders are skipped too.
- On a POSIX host the shell binding is `bash` instead of `pwsh` (the DSH preset disables `tool-bash` on win32 and `tool-pwsh` elsewhere); this host ships the Windows roster.
- `web_search` is not declared: the DSH `ptc` preset gates it on a configured search provider (`web.searchReady`), and none is mounted here — mirror DSH's own admission rule.
- `read_image` is not declared: its deferred image delivery has no MCP analog in ZCode. Inspect images from inside a program by other means (for example, read dimensions via the shell) or leave PTC mode.
- File sandbox enforcement is partial on this host (no confining backend): programs and tools run with full access, and results report `sandbox.enforcement: "partial"`.
- Binding failures raise `ToolCallError` with `toolName` set — `try/except` to handle and continue. Tool arguments must be lossless JSON; oversized returns (>32768 bytes) fail with `output-limit`, non-JSON returns with `invalid-output`.

## Workflow

1. Prefer ONE well-structured program per goal step: batch independent reads/searches under `asyncio.gather`, sequence dependent work with `await`.
2. Extract only what you need into the returned value — intermediate results never enter the conversation.
3. Check the `[exit code: N]` marker semantics of `pwsh` results (`exitCode`, `timedOut`) before moving on; investigate failures before continuing.
4. Mutating operations (`write`, `edit`, `pwsh`) run exclusively; do not rely on their results arriving concurrently with others.
"""

MINIMAL_SKILL = """---
name: minimal-mode
description: DeepSeek Harness (DSH) agent preset "minimal" mode for ZCode — persistent-shell-only assistant. Load when the session is in minimal mode or the user asks for minimal mode: use the persistent shell for everything; text-file reads and file mutation tools are denied.
---

You are a helpful software engineer assistant.

This is the complete persona: the DSH minimal preset mounts no other prompt sections and suppresses runtime context. The session's visible surface is reduced accordingly.

## Tool surface (enforced)

The minimal preset mounts only a persistent shell plus image reading:

- **`Bash`** — the persistent shell (this host's Windows variant):

  > Run commands in a PowerShell shell
  > * When invoking this tool, the contents of the "command" parameter does NOT need to be XML-escaped.
  > * You don't have access to the internet via this tool.
  > * State is persistent across command calls and discussions with the user.
  > * Use native Windows paths (C:\\...) and $env:NAME variables; this is PowerShell, not bash.
  > * Please avoid commands that may produce a very large amount of output.
  > * Please run long lived commands in the background, e.g. 'Start-Job' or start a server with Start-Process.

  The shell is persistent: the current directory and exported environment variables persist across calls for this agent. Commands carry a 300000 ms timeout. On POSIX hosts the same roster ships as the bash persistent shell ('sed -n 10,25p /path/to/the/file' for line ranges; background with 'sleep 10 &').
- **`Read` on image files only** — the `read_image` equivalent (`read_image` mounts beside a disabled read in the DSH minimal preset): .png/.jpg/.jpeg/.webp/.gif files are readable; use it to inspect images. Do not install image libraries or create thumbnails to inspect an image.

`Read` on text files, `Write`, `Edit`, `Glob`, `Grep`, subagents, web tools and the todo tool are DENIED (they are absent from the DSH minimal preset's roster). Do all file inspection, search, and mutation through the shell: `Get-Content`, `Select-String`, `Set-Content`, and the like. Do not retry a denied tool another way around the mode — the denial is the preset's own policy.

## Working style

- Work directly through the shell, tersely; the persona carries no further instructions.
- To switch back, run `/agent-mode:normal`. Mode state lives in `%USERPROFILE%\\.zcode\\agent-mode.json`.
"""


def write_skill(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("wrote", path)


write_skill(os.path.join(PLUGIN, "skills", "ptc-mode", "SKILL.md"), PTC_SKILL)
write_skill(os.path.join(PLUGIN, "skills", "minimal-mode", "SKILL.md"), MINIMAL_SKILL)
