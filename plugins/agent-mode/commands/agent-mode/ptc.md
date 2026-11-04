---
description: Switch the session to Agent Mode PTC mode (DSH programmatic tool calling via run_code)
argument-hint: (optional: what to do next in PTC mode)
---

Switch this session into **PTC mode** (the DeepSeek Harness agent preset `ptc`).

Perform these steps in order:

1. Create the state file `%USERPROFILE%\.zcode\agent-mode.json` (create the `.zcode` directory if missing) containing exactly `{"mode": "ptc"}` — use the Write tool on that absolute path. This activates the enforcement hook and the `run_code` transport.
2. Invoke the Skill tool with skill `agent-mode:ptc-mode` to load the full PTC briefing (the `run_code` SDK, execution rules, and the PTC-only rule).
3. Reply with one short sentence confirming that PTC mode is active and that `run_code` is now the only directly callable tool.

If `$ARGUMENTS` contains a task, begin executing it in PTC mode after the confirmation.
