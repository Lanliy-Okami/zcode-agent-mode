# Agent Mode

Ports the **DeepSeek Harness (DSH)** agent presets to ZCode:

| Mode | DSH preset | Effect |
|---|---|---|
| **PTC mode** | `preset-ptc` (`/agent-mode:ptc`) | Only `run_code` (exposed as `mcp__plugin_agent-mode_agent-mode__run_code`) is directly callable; every other tool call is rejected before policy with DSH's verbatim message. Work happens inside Python programs that call the generated SDK (`await tools.name(args)`); only printed/returned values enter the conversation. |
| **Minimal mode** | `preset-minimal` (`/agent-mode:minimal`) | Persona is exactly *"You are a helpful software engineer assistant."*; only the persistent shell plus image reads remain — `Read`/`Write`/`Edit`/`Glob`/`Grep` etc. are denied. |
| **Normal** | — (`/agent-mode:normal`) | Restores ZCode defaults. |

## Components

- `mcp/run_code_server.py` + `mcp/ptc_bootstrap.py` — the `run_code` transport: fresh CPython subprocess per program, duplex JSON-lines channel (DSH's fd 3 via `socket.share`), registry tools `pwsh, read, write, edit, glob, grep, web_fetch` with DSH-verbatim descriptions and canonical outputs, DSH budgets (log 64 KiB, value 32 KiB, wall 600 s) and error taxonomy.
- `mcp/sdk_renderer.py` + `mcp/tool_schemas.py` — faithful port of DSH's `renderToolsSdkPy`; regenerate the SDK section with `python -X utf8 sdk_renderer.py`, then rerun `tests/assemble_skills.py`.
- `scripts/agent_mode_hook.py` — `PreToolUse` enforcement (PTC-only rule, minimal whitelist, state-file exemption) and `UserPromptSubmit` mode context.
- `skills/ptc-mode`, `skills/minimal-mode` — the model-facing briefings (DSH PTC-only instruction + generated SDK block; DSH minimal persona + persistent-shell description).
- `commands/agent-mode/{ptc,minimal,normal}.md` — the `/agent-mode:*` switches.

## State

Mode lives in `%USERPROFILE%\.zcode\agent-mode.json` as `{"mode": "ptc" | "minimal" | "normal"}`; a workspace copy at `<workspace>\.zcode\agent-mode.json` is honored only if the user file is absent.

## Tests (development host)

```
python -X utf8 -I tests\test_run_code_server.py   # transport + registry + budgets + gate
python -X utf8 -I tests\test_hook.py              # enforcement + context injection
python -X utf8 -I tests\validate_plugin.py        # manifest/catalog/resources
```

## Known divergences from DSH (by platform necessity)

- DSH's released PTC runtime is Node/TypeScript; this host ships the **Python flavor** (which DSH also implements, with the exact model-facing texts). Programs run in fresh CPython processes without the POSIX file-sandbox backend; file-sandbox enforcement is reported as `partial`.
- Host-integration-only capabilities cannot be reproduced from a plugin: `read_image`'s deferred image delivery, provider-gated `web_search` (omitted — mirroring DSH's own admission when no provider is configured), `present` (deliverables UI), `goal`/`schedule` (Host session/scheduler services), and `subagent`/`subagent_fork` (Zcode's native Agent tool is denied under PTC like any other direct call).
- `ask_user_question` blocks inside programs when the client supports MCP elicitation; otherwise it degrades to DSH's timed-mode pending shape (`{pending, callId, message}`) and the user's next message is the answer.
- Mode state is per session (owner-stamped state file, session id provided by the session banner); sessions without an id fall back to the global file.
- Loading this plugin's own mode briefings (`Skill` with an `agent-mode` skill) stays reachable inside any mode — the analog of DSH's harness-owned prompt sections; other skills load via the `skill` binding, as in DSH.
