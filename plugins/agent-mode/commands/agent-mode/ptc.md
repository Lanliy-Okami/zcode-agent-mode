---
description: Switch the session to Agent Mode PTC mode (DSH programmatic tool calling via run_code)
argument-hint: (optional: what to do next in PTC mode)
---

Switch this session into **PTC mode** (the DeepSeek Harness agent preset `ptc`).

Perform these steps in order:

1. Invoke the Skill tool with skill `agent-mode:ptc-mode` to load the full PTC briefing (the `run_code` SDK, execution rules, and the PTC-only rule). Do this BEFORE activating enforcement.
2. Create the state file `%USERPROFILE%\.zcode\agent-mode.json` (create the `.zcode` directory if missing) containing `{"mode": "ptc", "owner": "<session id>"}` — where `<session id>` is the id shown in the "Agent Mode session" banner of the injected context (omit the owner field only if no banner is present). Use the Write tool on that absolute path. This activates the enforcement hook and the `run_code` transport, which ZCode exposes as `mcp__plugin_agent-mode_agent-mode__run_code`; from now on that tool is the only one you may call directly (plus AskUserQuestion for user interaction).
3. Reply with one short sentence confirming that PTC mode is active.

If `$ARGUMENTS` contains a task, begin executing it in PTC mode after the confirmation — through `run_code` programs only.
