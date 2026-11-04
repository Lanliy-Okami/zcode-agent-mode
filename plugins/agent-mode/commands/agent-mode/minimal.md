---
description: Switch the session to Agent Mode minimal mode (DSH minimal preset: persistent shell only)
argument-hint: (optional: what to do next in minimal mode)
---

Switch this session into **minimal mode** (the DeepSeek Harness agent preset `minimal`).

Perform these steps in order:

1. Invoke the Skill tool with skill `agent-mode:minimal-mode` to load the minimal briefing. Do this BEFORE activating enforcement.
2. Create the state file `%USERPROFILE%\.zcode\agent-mode.json` (create the `.zcode` directory if missing) containing `{"mode": "minimal", "owner": "<session id>"}` — where `<session id>` is the id shown in the "Agent Mode session" banner of the injected context (omit the owner field only if no banner is present). Use the Write tool on that absolute path. This activates the enforcement hook.
3. Reply with one short sentence confirming that minimal mode is active: only the persistent shell (Bash) plus image reads remain available.

If `$ARGUMENTS` contains a task, begin executing it in minimal mode after the confirmation — through the shell only.
