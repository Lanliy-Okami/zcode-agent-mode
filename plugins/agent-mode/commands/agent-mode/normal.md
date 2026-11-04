---
description: Leave Agent Mode and restore ZCode's default agent behavior
---

Leave **Agent Mode** and restore ZCode's default behavior.

Perform these steps in order:

1. Overwrite `%USERPROFILE%\.zcode\agent-mode.json` (use the Write tool on that absolute path) with exactly `{"mode": "normal"}`. Any value other than `ptc`/`minimal` disables Agent Mode enforcement.
2. Reply with one short sentence confirming that Agent Mode is off and ZCode's default tools and behavior are restored.
