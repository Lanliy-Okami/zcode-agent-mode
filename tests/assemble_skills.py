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

`run_code` is the only tool you can call directly — a tool call naming any other tool fails. Reach every tool the SDK declares below from inside the program.

ZCode exposes the reserved transport as **`mcp__agent-mode__run_code`**. A direct call to any other tool is rejected before policy with: `only \\`run_code\\` is callable directly — call \\`<name>\\` from inside a \\`run_code\\` program instead`.

## Transport contract

- The program is the body of an async Python function. Top-level await and return are supported. Emit output with print(...) or return a lossless JSON value. Only printed values and the returned value become program output. Each call runs in a fresh Python process; variables and imported modules do not persist across calls. Relative paths use the supplied working directory; only TMPDIR is set in the environment. Direct filesystem access follows this execution's sandbox policy.
- Each program starts in the Session's current directory. Running programs keep their initial directory.
- A sandbox escalation approves this complete program for one execution only. Nested tools retain their own policies and approvals. Request wider access only after evidence of a denial. Earlier effects may already have completed: inspect them before explicitly retrying. Programs are never replayed automatically.
- The wall deadline is 600000 ms. Programs that exceed it fail with `code run failed (timeout): …`; captured output precedes the error text.
- `run_code` requires Agent Mode PTC mode (`{{"mode": "ptc"}}` in `%USERPROFILE%\\.zcode\\agent-mode.json`, set by `/agent-mode:ptc`). Without it the transport refuses.

{sdk}

## Registry notes

- The declared bindings are the model-facing surface: `pwsh`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch` — the DSH `ptc` preset's core shell/file/search/web tools, with DSH's verbatim descriptions and canonical output shapes. Safe (read-only) calls may overlap under `asyncio.gather`; mutating calls (`pwsh`, `write`, `edit`) run alone, in submission order.
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
