---
name: ptc-mode
description: DeepSeek Harness (DSH) agent preset "ptc" mode for ZCode — programmatic tool calling. Load when the session is in PTC mode or the user asks for PTC / run_code / programmatic tool calling: every tool call must go through a run_code program using the generated Python SDK.
---

# PTC mode (DeepSeek Harness agent preset)

`run_code` is the only tool you can call directly — a tool call naming any other tool fails. Reach every tool the SDK declares below from inside the program.

ZCode exposes the reserved transport as **`mcp__plugin_agent-mode_agent-mode__run_code`** (plugin-prefixed MCP naming). A direct call to any other tool is rejected before policy with: `only \`run_code\` is callable directly — call \`<name>\` from inside a \`run_code\` program instead`.

## Transport contract

- The program is the body of an async Python function. Top-level await and return are supported. Emit output with print(...) or return a lossless JSON value. Only printed values and the returned value become program output. Each call runs in a fresh Python process; variables and imported modules do not persist across calls. Relative paths use the supplied working directory; only TMPDIR is set in the environment. Direct filesystem access follows this execution's sandbox policy.
- Each program starts in the Session's current directory. Running programs keep their initial directory.
- A sandbox escalation approves this complete program for one execution only. Nested tools retain their own policies and approvals. Request wider access only after evidence of a denial. Earlier effects may already have completed: inspect them before explicitly retrying. Programs are never replayed automatically.
- The wall deadline is 600000 ms. Programs that exceed it fail with `code run failed (timeout): …`; captured output precedes the error text.
- `run_code` requires Agent Mode PTC mode (`{"mode": "ptc"}` in `%USERPROFILE%\.zcode\agent-mode.json`, set by `/agent-mode:ptc`). Without it the transport refuses.

## Writing code for run_code

`run_code` takes two required arguments: `code`, the program to execute, and `description`, a short summary of what the program does. At run time exactly two of the names declared below are bound: `tools` and `ToolCallError`. Everything else is a STATIC STUB describing argument and return types — in particular the `TypedDict` classes do NOT exist at run time, so build arguments as plain `dict`/`list` JSON values: `await tools.name({"field": 1})`, never `FooArgs(field=1)`, which raises `NameError`. Inside the program:

- Call tools as `await tools.name(args)` — subscript access for exotic, reserved, or underscore-leading names: `await tools["my-tool"](args)`. Every call resolves to the tool's typed canonical JSON value (each method's return type below). Tool arguments must be lossless JSON.
- A FAILED tool call raises `ToolCallError`, whose `toolName` identifies the failed tool and whose message is human-readable — wrap in `try/except` to handle and continue.
- Independent read-only calls MAY overlap under `asyncio.gather` (safe calls run concurrently; mutating calls run alone, in submission order). Sequence dependent work with `await`.
- Only values emitted as program output enter the conversation. A successful tool result containing an image is attached after the run so you can inspect it on the next step; every other intermediate result stays out of the conversation, so extract just what you need.

The available tools:

```python
from typing import Literal, NotRequired, Protocol, TypedDict

class ToolCallError(Exception):
    toolName: str

class EditArgs(TypedDict):
    # Path to edit, resolved by the filesystem backend. Provide `file_path` before `old_string` and `new_string` in the arguments.
    file_path: str
    # Literal text to replace.
    old_string: str
    # Literal replacement text. Use an empty string to delete the match.
    new_string: str
    # Replace all matches. Defaults to false; when false, old_string must appear exactly once.
    replace_all: NotRequired[bool]
    # Additional keys beyond those declared are allowed.
class EditOutput(TypedDict):
    path: str
    before: str | None
    after: str | None
    # Additional keys beyond those declared are allowed.
class GlobArgs(TypedDict):
    # Glob pattern to match file paths against (e.g. "**/*.ts", "src/**/*.test.js"). A pattern with no "/" matches the basename at any depth, so "*" and "*.ts" both search the whole tree; include a separator to anchor the depth.
    pattern: str
    # Directory to search in. Defaults to the session workspace; a relative path resolves against it.
    path: NotRequired[str]
    # Additional keys beyond those declared are allowed.
class GlobOutput(TypedDict):
    root: str
    paths: list[str]
    # Additional keys beyond those declared are allowed.
class GrepArgs(TypedDict):
    # Regular expression to search for (ripgrep syntax).
    pattern: str
    # File or directory to search. Defaults to the session workspace; a relative path resolves against it.
    path: NotRequired[str]
    # One glob filter for which files to search (e.g. "*.ts", "*.{js,jsx}"). Not a list; negation is not supported.
    include: NotRequired[str]
    # Additional keys beyond those declared are allowed.
class GrepOutputMatches(TypedDict):
    path: str
    lineNumber: int
    line: str
    # Additional keys beyond those declared are allowed.
class GrepOutput(TypedDict):
    matches: list[GrepOutputMatches]
    # Additional keys beyond those declared are allowed.
class PwshArgs(TypedDict):
    # The PowerShell command to execute.
    command: str
    # Clear, concise description of what this command does in active voice, 5-10 words (shown in the UI). Examples: "ls" → "List files in current directory"; "git status" → "Show working tree status"; "npm install" → "Install package dependencies".
    description: str
    # Timeout in milliseconds. The executor applies its configured default and cap, and kills the command on expiry.
    timeoutMs: NotRequired[float]
    # Working directory for this command. Defaults to the session workspace; a relative path is resolved against it.
    workdir: NotRequired[str]
    # Additional keys beyond those declared are allowed.
class PwshOutputStdout(TypedDict):
    text: str
    truncated: bool
    # Additional keys beyond those declared are allowed.
class PwshOutputStderr(TypedDict):
    text: str
    truncated: bool
    # Additional keys beyond those declared are allowed.
class PwshOutput(TypedDict):
    kind: Literal["foreground"]
    cwd: str
    exitCode: int | None
    signal: str | None
    timedOut: bool
    aborted: bool
    stdout: PwshOutputStdout
    stderr: PwshOutputStderr
    # Additional keys beyond those declared are allowed.
class ReadArgs(TypedDict):
    # Path to read, resolved by the filesystem backend.
    file_path: str
    # 1-based first line to return. Defaults to 1.
    offset: NotRequired[float]
    # Maximum number of lines to return. Defaults to 2000.
    limit: NotRequired[float]
    # Additional keys beyond those declared are allowed.
class ReadOutputLines(TypedDict):
    number: int
    text: str
    # Additional keys beyond those declared are allowed.
class ReadOutput(TypedDict):
    path: str
    offset: int
    lines: list[ReadOutputLines]
    totalLines: int
    # Additional keys beyond those declared are allowed.
class WebFetchArgs(TypedDict):
    # The HTTP(S) URL to fetch.
    url: str
    # Additional keys beyond those declared are allowed.
class WebFetchOutputBody(TypedDict):
    kind: Literal["html", "text"]
    content: str
    # Additional keys beyond those declared are allowed.
class WebFetchOutput(TypedDict):
    url: str
    statusCode: int
    body: WebFetchOutputBody
    truncated: bool
    # Additional keys beyond those declared are allowed.
class WriteArgs(TypedDict):
    # Path to write, resolved by the filesystem backend. Provide `file_path` before `content` in the arguments.
    file_path: str
    # Full UTF-8 text content to write.
    content: str
    # Additional keys beyond those declared are allowed.
class WriteOutput(TypedDict):
    path: str
    operation: Literal["create", "update"]
    before: str | None
    after: str
    # Additional keys beyond those declared are allowed.

class Tools(Protocol):
    async def edit(self, args: EditArgs) -> EditOutput:
        """Edit an existing UTF-8 text file by replacing literal text."""
    async def glob(self, args: GlobArgs) -> GlobOutput:
        """Find files, not directories, whose paths match a glob pattern, including hidden and ignored files. Returns up to 100 paths in modification-time order; a larger result keeps the first paths and reports where the complete list was saved."""
    async def grep(self, args: GrepArgs) -> GrepOutput:
        """Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. Returns up to 250 matches; a larger result reports where the complete match list was saved."""
    async def pwsh(self, args: PwshArgs) -> PwshOutput:
        """Execute a PowerShell command (`pwsh -Command`) and return its stdout/stderr. Each call runs in a fresh pwsh process; pass `workdir` instead of using `cd`. Paths use native Windows form (`C:\\...`); read environment variables with `$env:NAME`. Managed `$env:DSH_*` variables expose current harness environment facts. Long output is truncated to its tail; the full output is saved to a file whose path is reported when available. On Windows a force-killed command settles as `[exit code: 1]` without a signal marker — treat it as an interruption, not a command failure. Provide `description` before `command` in the arguments. Before any delete or move, verify that the resolved absolute target path is the intended one; never run it against a computed path you have not checked. Do not assign to automatic variables such as `$HOME`; variable names are case-insensitive, so `$home` is the same read-only variable. Commands may run under a file sandbox; a blocked file operation is reported as `[sandbox: file access denied under <mode> mode]`, a policy denial: do not retry another way."""
    async def read(self, args: ReadArgs) -> ReadOutput:
        """Read a UTF-8 text file and return line-numbered content."""
    async def web_fetch(self, args: WebFetchArgs) -> WebFetchOutput:
        """Fetch the content of a specific HTTP(S) URL and return it decoded to text."""
    async def write(self, args: WriteArgs) -> WriteOutput:
        """Create or fully replace a UTF-8 text file."""

tools: Tools
```

## Registry notes

- The declared bindings are the model-facing surface: `pwsh`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch` — the DSH `ptc` preset's core shell/file/search/web tools, with DSH's verbatim descriptions and canonical output shapes. Safe (read-only) calls may overlap under `asyncio.gather`; mutating calls (`pwsh`, `write`, `edit`) run alone, in submission order.
- **Projected MCP bindings**: like DSH, the registry binds every agent-visible tool — the tools of the MCP servers configured in ZCode (user/workspace scope) are projected as additional bindings and listed at the top of each session ("PTC projected MCP bindings"). Call them by bare name (`await tools.get_me({})`) or subscript; their arguments come lossless-JSON as plain dict/list (the projected list names each tool's required args). They classify FAIL-CLOSED EXCLUSIVE (DSH's rule for undeclared tools): they run alone, in submission order, never overlapping. A server that cannot be reached is skipped and noted in the same context block; servers whose commands carry unresolvable `${...}` plugin placeholders are skipped too.
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
