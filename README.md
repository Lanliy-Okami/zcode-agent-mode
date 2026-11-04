<h1 align="center">Agent Mode</h1>

<p align="center"><b>将 DeepSeek Harness（DSH）Agent 预设移植到 ZCode 的插件</b><br/>
A ZCode plugin porting the DeepSeek Harness (DSH) agent presets: <b>PTC mode</b> &amp; <b>Minimal mode</b>.</p>

---

English | [中文](#中文)

## What it does

| Mode | Command | Effect |
|---|---|---|
| **PTC mode** | `/agent-mode:ptc` | Only `run_code` (exposed as `mcp__plugin_agent-mode_agent-mode__run_code`) is directly callable. A direct call to any other tool is rejected before policy with DSH's verbatim message: `only \`run_code\` is callable directly — call \`<name>\` from inside a \`run_code\` program instead`. Work happens inside Python programs that call the generated SDK (`await tools.name(args)`); only printed/returned values enter the conversation. |
| **Minimal mode** | `/agent-mode:minimal` | The persona is exactly *"You are a helpful software engineer assistant."* (DSH `complete: true`). Only the persistent shell plus image reads remain; `Read`/`Write`/`Edit`/`Glob`/`Grep` etc. are denied. |
| **Normal** | `/agent-mode:normal` | Restores ZCode's default behavior. |

Fidelity highlights (ported line-by-line from the DSH sources):

- The PTC-only instruction, the generated Python SDK section (a faithful port of `renderToolsSdkPy` from `packages/core/tool-executor-ptc/src/py-types.ts`), and the Python runtime's execution instructions — all verbatim.
- Tool registry `pwsh, read, write, edit, glob, grep, web_fetch` with DSH-verbatim tool descriptions, parameter descriptions and canonical output shapes.
- DSH semantics: fresh CPython subprocess per program, duplex JSON-lines protocol channel (DSH's fd 3), safe read-only calls overlap while mutating calls run alone in submission order, `ToolCallError(toolName=…)`, log capture truncated at 64 KiB with DSH's marker, 32 KiB completion-value budget (`invalid-output` / `output-limit`), 600 s wall deadline, and the `code run failed (kind): …` error shape.
- The minimal preset's persistent-shell tool description (Windows/bash variants) verbatim.

## Install (ZCode ≥ with Plugin Marketplace)

1. Open **Plugin Marketplace → Add → Add Plugin Marketplace** (插件市场 → 添加 → 添加插件市场).
2. Paste this repository as the marketplace source: `https://github.com/Lanliy-Okami/zcode-agent-mode`（把本仓库地址粘贴进去）.
3. Go to **Personal → zcode-agent-mode → Agent Mode → Install**（个人 → 该市场 → Agent Mode → 安装）.
4. Manage it later under **Settings → Plugins** (设置 → 插件). The plugin's hooks and its MCP server connect automatically.

No build step and no extra tooling is required — the plugin runs on the Python 3.10+ already present on the host (`python` on `PATH`).

## Try it

- `/agent-mode:ptc`, then: *"List every .py file in the workspace and count the total lines."*
  Expected: the model does not call any native tool directly; it calls one `run_code` program (`glob` + `read` overlapped under `asyncio.gather`) and returns only the statistics. A stray direct call gets DSH's exact rejection text.
- `/agent-mode:minimal`, then: *"Read the first lines of README.md."*
  Expected: the model uses the persistent shell (`Get-Content`) instead of the `Read` tool.
- `/agent-mode:normal` leaves Agent Mode.

Mode state lives in `%USERPROFILE%\.zcode\agent-mode.json` as `{"mode": "ptc" | "minimal" | "normal"}`; a workspace copy at `<workspace>\.zcode\agent-mode.json` is honored only when the user file is absent.

## Layout

```
marketplace.json            # ZCode marketplace catalog (install source)
plugins/agent-mode/
  .zcode-plugin/plugin.json # ZCode plugin manifest
  .mcp.json                 # run_code MCP server (stdio, stdlib-only Python)
  mcp/
    run_code_server.py      # PTC transport: tool registry, budgets, DSH result rendering
    ptc_bootstrap.py        # child side: async-function-body execution, fd-3 channel
    tool_schemas.py         # DSH tool contracts (verbatim descriptions/schemas)
    sdk_renderer.py         # faithful port of DSH renderToolsSdkPy
  scripts/agent_mode_hook.py# PreToolUse enforcement + UserPromptSubmit mode context
  skills/ptc-mode/          # PTC briefing: PTC-only rule + generated SDK block
  skills/minimal-mode/      # minimal briefing: persona + persistent shell
  commands/agent-mode/      # /agent-mode:{ptc,minimal,normal}
  README.md                 # component and maintenance notes
```

## Known divergences (platform necessity)

- DSH's released PTC runtime is Node/TypeScript; this host ships the **Python flavor** (which DSH also implements, with the exact model-facing texts). Programs run in fresh CPython processes without the POSIX file-sandbox backend; sandbox enforcement is reported as `partial`.
- `read_image`'s deferred image delivery and provider-gated `web_search` are not mounted (mirroring DSH's own tool admission when no provider is configured).
- Mode is a global switch (state file) rather than a per-session harness mode.

## Development tests

```
python -X utf8 -I tests\test_run_code_server.py   # transport + registry + budgets + gate
python -X utf8 -I tests\test_hook.py              # enforcement + context injection
python -X utf8 -I tests\validate_plugin.py        # manifest/catalog/resources
```

## License

MIT — see [LICENSE](LICENSE).

---

<a id="中文"></a>

## 中文说明

**Agent Mode** 将 DeepSeek Harness（DSH）的 Agent 预设移植到 ZCode：

- **PTC 模式**（`/agent-mode:ptc`）：程序化工具调用。`run_code` 是唯一可直调的工具，其余工具直调会被钩子以 DSH 原文拒绝；模型按生成的 Python SDK 在程序内调用工具（`await tools.name(args)`），只有打印/返回的内容进入上下文。工具注册表 `pwsh, read, write, edit, glob, grep, web_fetch` 的描述与输出结构与 DSH 逐字一致；日志/预算/错误分类/结果渲染与 DSH 一致。
- **极简模式**（`/agent-mode:minimal`）：persona 即 DSH 原文 "You are a helpful software engineer assistant."；仅保留持久 shell 与图片读取，其余工具全部拒绝。
- **正常模式**（`/agent-mode:normal`）：恢复 ZCode 默认行为。

### 安装

1. 打开 **插件市场 → 添加 → 添加插件市场**。
2. 粘贴本仓库地址作为市场来源：`https://github.com/Lanliy-Okami/zcode-agent-mode`。
3. **个人 → zcode-agent-mode → Agent Mode → 安装**；之后在 **设置 → 插件** 管理。插件钩子自动启用，MCP 服务器自动连接。

无需构建，也无需额外安装任何开发工具；插件使用宿主机已有的 Python 3.10+（`PATH` 上的 `python`）。

### 与 DSH 的差异（平台所限）

DSH 发布版 PTC 运行时是 Node/TypeScript；本插件提供的是 DSH 同样实现了的 **Python 风味**（模型可见文本逐字一致），程序在全新 CPython 子进程中运行，文件沙箱强制标记为 `partial`；`read_image` 的延迟图片投递与依赖搜索供应商的 `web_search` 未挂载（与 DSH 在无供应商时自身的准入规则一致）；模式为全局开关而非会话级 harness 模式。

### 许可证

MIT。
