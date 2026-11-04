# -*- coding: utf-8 -*-
"""Agent Mode MCP server: the DSH PTC-mode `run_code` transport for ZCode.

Ports the model-visible contract of the DeepSeek Harness PTC executor
(packages/core/tool-executor-ptc) + Python PTC runtime
(packages/experimental/ptc-runtime-python) to a ZCode MCP server:

- A single reserved tool `run_code` (exposed by ZCode as
  `mcp__plugin_agent-mode_agent-mode__run_code`).
  Its description, parameter descriptions, and escalation guidance are assembled
  exactly as ptc.ts does for the Python flavor.
- Each call spawns a FRESH CPython subprocess running ptc_bootstrap.py: the program
  is the body of an async function; binding calls travel as JSON-lines frames over
  one duplex protocol channel (DSH's fd 3, a socketpair here) and are dispatched
  through this server's tool registry.
- Tool registry: pwsh, read, write, edit, glob, grep, web_fetch — the DSH `ptc`
  preset's core file/shell/web tools with verbatim DSH descriptions and canonical
  output shapes. Safe (read-only) calls may overlap; mutating calls run alone, in
  submission order (DSH's native concurrency contract), capped at maxParallelSubCalls.
- Result rendering, log capture/truncation, the error taxonomy (exception /
  invalid-output / output-limit / timeout / worker-exit), and budgets follow DSH.

Only the Python standard library is used. Run with `python -X utf8 -I`.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import fnmatch
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tool_schemas  # noqa: E402

SERVER_NAME = "agent-mode"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

MAX_PARALLEL_SUB_CALLS = 10  # DSH tool-executor-ptc default maxParallelSubCalls
MAX_WALL_MS = 600000  # DSH Python runtime maxWallMs default
MAX_VALUE_BYTES = 32768  # DSH Python runtime maxValueBytes default
MAX_LOG_BYTES = 65536  # DSH Python runtime maxLogBytes default
LOG_TRUNCATION_MARKER = "[dsh-ptc-runtime-python] log capture truncated at 65536 bytes"
SHELL_DEFAULT_TIMEOUT_MS = 120000
SHELL_MAX_TIMEOUT_MS = 600000
SHELL_MAX_OUTPUT_CHARS = 16000
READ_LIMIT = 2000
READ_MAX_LINE_LENGTH = 2000
READ_MAX_BYTES = 51200
GLOB_MAX_RESULTS = 100
GREP_MAX_MATCHES = 250
GREP_MAX_LINE_BYTES = 2000
FETCH_MAX_OUTPUT_CHARS = 200000
FETCH_TIMEOUT_S = 60

RUN_CODE_TOOL = "run_code"

EXCLUSIVE_TOOLS = {"pwsh", "write", "edit"}  # mutating calls run alone, in submission order

RUN_CODE_DESCRIPTION = tool_schemas.build_run_code_description()
RUN_CODE_INPUT_SCHEMA = tool_schemas.build_run_code_input_schema()

STANDING_SANDBOX_MODE = "danger-full-access"  # no confining backend on this host
SANDBOX_ENFORCEMENT = "partial"


class ToolFailure(Exception):
    """A registry tool failed; the binding raises ToolCallError with this message."""


class CodeRunFailed(Exception):
    """The program run failed; message follows DSH's CodeRunFailedError shape."""

    def __init__(self, kind: str, message: str, logs: list[str], sandbox: dict | None) -> None:
        logs_text = f"\nCaptured output:\n" + "\n".join(logs) if logs else ""
        sandbox_text = ""
        if sandbox is not None:
            enforcement = sandbox.get("enforcement")
            sandbox_text = (
                f"\nFile sandbox: {sandbox.get('mode')}"
                + (f"; enforcement: {enforcement}" if enforcement else "")
                + ("; operation denied" if sandbox.get("denied") else "")
                + "."
            )
        super().__init__(f"code run failed ({kind}): {message}{logs_text}{sandbox_text}")


# ---------------------------------------------------------------- state file

def read_mode() -> str | None:
    """Resolve the active Agent Mode; mirrors scripts/agent_mode_hook.py."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates = [os.path.join(home, ".zcode", "agent-mode.json")]
    override = os.environ.get("AGENT_MODE_STATE_FILE")
    if override:
        candidates.insert(0, override)
    candidates.append(os.path.join(os.getcwd(), ".zcode", "agent-mode.json"))
    for path in candidates:
        try:
            with io.open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        mode = data.get("mode") if isinstance(data, dict) else None
        return str(mode) if mode in ("ptc", "minimal") else None
    return None


# ---------------------------------------------------------------- registry tools

def _resolve(path: str) -> str:
    if not os.path.isabs(path):
        return os.path.abspath(os.path.join(os.getcwd(), path))
    return os.path.abspath(path)


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[-max_chars:], True  # DSH keeps the tail of long output


async def _tool_pwsh(args: dict) -> dict:
    command = args.get("command")
    description = args.get("description")
    if not isinstance(command, str) or not command:
        raise ToolFailure("pwsh failed: invalid command: expected a non-empty string")
    if not isinstance(description, str) or description.strip() == "":
        raise ToolFailure("pwsh failed: invalid description: expected a non-empty string")
    timeout_ms = args.get("timeoutMs", SHELL_DEFAULT_TIMEOUT_MS)
    if not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0:
        timeout_ms = SHELL_DEFAULT_TIMEOUT_MS
    timeout_ms = min(timeout_ms, SHELL_MAX_TIMEOUT_MS)
    workdir = args.get("workdir")
    cwd = _resolve(workdir) if isinstance(workdir, str) and workdir else os.getcwd()
    if not os.path.isdir(cwd):
        raise ToolFailure(f"pwsh failed: workdir does not exist: {cwd}")
    shell = shutil.which("pwsh") or "powershell.exe"
    timed_out = False
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [shell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            cwd=cwd,
            timeout=timeout_ms / 1000.0,
        )
        exit_code = completed.returncode
        stdout_bytes, stderr_bytes = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        exit_code = 1  # DSH: a force-killed Windows command settles as [exit code: 1]
        stdout_bytes = expired.stdout or b""
        stderr_bytes = expired.stderr or b""
    except OSError as error:
        raise ToolFailure(f"pwsh failed: {error}") from None
    stdout_text, stdout_truncated = _clip(stdout_bytes.decode("utf-8", errors="replace"), SHELL_MAX_OUTPUT_CHARS)
    stderr_text, stderr_truncated = _clip(stderr_bytes.decode("utf-8", errors="replace"), SHELL_MAX_OUTPUT_CHARS)
    return {
        "kind": "foreground",
        "cwd": cwd,
        "exitCode": exit_code,
        "signal": None,
        "timedOut": timed_out,
        "aborted": False,
        "stdout": {"text": stdout_text, "truncated": stdout_truncated},
        "stderr": {"text": stderr_text, "truncated": stderr_truncated},
    }


def _tool_read(args: dict) -> dict:
    file_path = args.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise ToolFailure("read failed: invalid file_path: expected a non-empty string")
    resolved = _resolve(file_path)
    try:
        with io.open(resolved, "rb") as handle:
            data = handle.read(READ_MAX_BYTES + 1)
    except OSError as error:
        raise ToolFailure(f"read failed: {error}") from None
    text = data[:READ_MAX_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)  # line count within the byte-cap window
    offset = args.get("offset", 1)
    limit = args.get("limit", READ_LIMIT)
    offset_i = int(offset) if isinstance(offset, (int, float)) and offset >= 1 else 1
    limit_i = int(min(limit, READ_LIMIT)) if isinstance(limit, (int, float)) and limit >= 1 else READ_LIMIT
    window = lines[offset_i - 1 : offset_i - 1 + limit_i]
    out_lines = [
        {"number": offset_i + index, "text": line[:READ_MAX_LINE_LENGTH]}
        for index, line in enumerate(window)
    ]
    return {"path": resolved, "offset": offset_i, "lines": out_lines, "totalLines": total_lines}


def _tool_write(args: dict) -> dict:
    file_path = args.get("file_path")
    content = args.get("content")
    if not isinstance(file_path, str) or not file_path:
        raise ToolFailure("write failed: invalid file_path: expected a non-empty string")
    if not isinstance(content, str):
        raise ToolFailure("write failed: invalid content: expected a string")
    resolved = _resolve(file_path)
    operation = "update" if os.path.exists(resolved) else "create"
    before = None
    if operation == "update":
        try:
            with io.open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                before = handle.read()
        except OSError as error:
            raise ToolFailure(f"write failed: {error}") from None
    try:
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with io.open(resolved, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except OSError as error:
        raise ToolFailure(f"write failed: {error}") from None
    return {"path": resolved, "operation": operation, "before": before, "after": content}


def _tool_edit(args: dict) -> dict:
    file_path = args.get("file_path")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))
    if not isinstance(file_path, str) or not file_path:
        raise ToolFailure("edit failed: invalid file_path: expected a non-empty string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        raise ToolFailure("edit failed: invalid old_string/new_string: expected strings")
    if old_string == "":
        raise ToolFailure("edit failed: old_string must not be empty")
    resolved = _resolve(file_path)
    try:
        with io.open(resolved, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError as error:
        raise ToolFailure(f"edit failed: {error}") from None
    occurrences = content.count(old_string)
    if occurrences == 0:
        raise ToolFailure("edit failed: old_string not found in file")
    if not replace_all and occurrences > 1:
        raise ToolFailure(
            f"edit failed: old_string must appear exactly once in the file (found {occurrences} occurrences); "
            "pass replace_all to replace every match"
        )
    updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    try:
        with io.open(resolved, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
    except OSError as error:
        raise ToolFailure(f"edit failed: {error}") from None
    return {"path": resolved, "before": old_string, "after": new_string}


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a glob with `**` semantics to a regex over '/'-separated paths."""
    out = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 3] == "**/":
                out += "(?:.*/)?"
                i += 3
            elif pattern[i : i + 2] == "**":
                out += ".*"
                i += 2
            else:
                out += "[^/]*"
                i += 1
        elif ch == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(ch)
            i += 1
    return re.compile("^" + out + "$")


def _tool_glob(args: dict) -> dict:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolFailure("glob failed: invalid pattern: expected a non-empty string")
    root_arg = args.get("path")
    root = _resolve(root_arg) if isinstance(root_arg, str) and root_arg else os.getcwd()
    if not os.path.isdir(root):
        raise ToolFailure(f"glob failed: path does not exist: {root}")
    found: list[tuple[float, str]] = []
    if "/" in pattern:
        regex = _glob_to_regex(pattern)
        match = lambda relative, filename: regex.match(relative) is not None
    else:
        regex = _glob_to_regex(pattern)
        match = lambda relative, filename: regex.match(filename) is not None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in filenames:
            absolute = os.path.join(dirpath, filename)
            relative = os.path.relpath(absolute, root).replace("\\", "/")
            if match(relative, filename):
                try:
                    found.append((os.path.getmtime(absolute), absolute))
                except OSError:
                    continue
    # Modification-time order (newest first).
    found.sort(key=lambda entry: entry[0], reverse=True)
    all_paths = [absolute for _, absolute in found]
    if len(all_paths) > GLOB_MAX_RESULTS:
        try:
            # sampleOverCapGlobResults=false: keep the first paths and save the complete list.
            with io.open(os.path.join(root, "glob-results.txt"), "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(all_paths) + "\n")
        except OSError:
            pass  # The spill is best-effort; the capped result stays authoritative.
    return {"root": root, "paths": all_paths[:GLOB_MAX_RESULTS]}


def _brace_expand(glob_filter: str) -> list[str]:
    """Expand a single `{a,b}` group (DSH include examples use one brace level)."""
    match = re.search(r"\{([^{}]*)\}", glob_filter)
    if match is None:
        return [glob_filter]
    head, tail = glob_filter[: match.start()], glob_filter[match.end() :]
    return [head + option + tail for option in match.group(1).split(",")]


def _tool_grep(args: dict) -> dict:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolFailure("grep failed: invalid pattern: expected a non-empty string")
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise ToolFailure(f"grep failed: invalid regex: {error}") from None
    path_arg = args.get("path")
    include = args.get("include")
    target = _resolve(path_arg) if isinstance(path_arg, str) and path_arg else os.getcwd()
    include_names = _brace_expand(include) if isinstance(include, str) and include else None
    files: list[str] = []
    if os.path.isfile(target):
        files.append(target)
    elif os.path.isdir(target):
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames.sort()
            for filename in sorted(filenames):
                if include_names is not None and not any(fnmatch.fnmatch(filename, f) for f in include_names):
                    continue
                files.append(os.path.join(dirpath, filename))
    matches: list[dict] = []
    total = 0
    for file_path in files:
        try:
            with io.open(file_path, "rb") as handle:
                head = handle.read(4096)
                if b"\x00" in head:
                    continue  # ripgrep skips binary files
                handle.seek(0)
                data = handle.read()
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line) is not None:
                total += 1
                if len(matches) < GREP_MAX_MATCHES:
                    matches.append(
                        {
                            "path": file_path,
                            "lineNumber": line_number,
                            "line": line[:GREP_MAX_LINE_BYTES],
                        }
                    )
    if total > GREP_MAX_MATCHES:
        spill_root = target if os.path.isdir(target) else os.path.dirname(target)
        try:
            with io.open(os.path.join(spill_root, "grep-results.txt"), "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"Found {total} matches\n")
                for match in matches:
                    handle.write(f"{match['path']}:{match['lineNumber']}: {match['line']}\n")
        except OSError:
            pass
    return {"matches": matches}


def _tool_web_fetch(args: dict) -> dict:
    url = args.get("url")
    if not isinstance(url, str) or not url:
        raise ToolFailure("web_fetch failed: invalid url: expected a non-empty string")
    if not url.startswith(("http://", "https://")):
        raise ToolFailure("web_fetch failed: only http(s) URLs are supported")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Agent Mode PTC)"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            status_code = response.status
            content_type = (response.headers.get("Content-Type") or "").lower()
            payload = response.read(FETCH_MAX_OUTPUT_CHARS + 1)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ToolFailure(f"web_fetch failed: {error}") from None
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type)
    if match is not None:
        charset = match.group(1)
    body = payload[:FETCH_MAX_OUTPUT_CHARS].decode(charset, errors="replace")
    kind = "html" if "html" in content_type else "text"
    return {
        "url": url,
        "statusCode": status_code,
        "body": {"kind": kind, "content": body},
        "truncated": len(payload) > FETCH_MAX_OUTPUT_CHARS,
    }


REGISTRY = {
    "pwsh": _tool_pwsh,
    "read": _tool_read,
    "write": _tool_write,
    "edit": _tool_edit,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "web_fetch": _tool_web_fetch,
}


# ---------------------------------------------------------------- program execution

class ProgramRun:
    """One run_code execution: fresh subprocess, frame pump, ordered dispatch."""

    def __init__(self) -> None:
        self.logs: list[str] = []
        self.logs_bytes = 0
        self.logs_truncated = False
        self.done: dict | None = None
        self.done_event = threading.Event()
        self.process: subprocess.Popen | None = None
        self.exclusive_lock = threading.Lock()
        self.parallel_semaphore = threading.BoundedSemaphore(MAX_PARALLEL_SUB_CALLS)
        self.reply_file: io.BufferedWriter | None = None
        self.reply_lock = threading.Lock()

    def append_log(self, chunk: str) -> None:
        if self.logs_truncated:
            return
        encoded = chunk.encode("utf-8")
        if self.logs_bytes + len(encoded) > MAX_LOG_BYTES:
            remaining = MAX_LOG_BYTES - self.logs_bytes
            if remaining > 0:
                self.logs.append(chunk[:remaining])
            self.logs.append(LOG_TRUNCATION_MARKER)
            self.logs_truncated = True
            return
        self.logs_bytes += len(encoded)
        self.logs.append(chunk)

    def send_reply(self, call_id: Any, ok: bool, value=None, error: str | None = None) -> None:
        frame: dict = {"type": "reply", "id": call_id, "ok": ok}
        if ok:
            frame["value"] = value
        else:
            frame["error"] = error or "tool call failed"
        data = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        with self.reply_lock:
            if self.reply_file is not None:
                try:
                    self.reply_file.write(data)
                    self.reply_file.flush()
                except OSError:
                    pass

    def dispatch(self, call_id: Any, name: str, args: Any) -> None:
        def run() -> None:
            try:
                tool = REGISTRY.get(name)
                if tool is None:
                    raise ToolFailure(f"unknown tool binding: {name}")
                call_args = args if isinstance(args, dict) else {}

                def invoke():
                    if isinstance(tool, _AsyncTool):
                        return tool.call(call_args)
                    return tool(call_args)

                # DSH native concurrency contract: mutating calls run alone, in
                # submission order; safe calls may overlap, capped at maxParallel.
                if name in EXCLUSIVE_TOOLS:
                    with self.exclusive_lock:
                        with self.parallel_semaphore:
                            value = invoke()
                else:
                    with self.parallel_semaphore:
                        value = invoke()
                self.send_reply(call_id, True, value=value)
            except ToolFailure as error:
                self.send_reply(call_id, False, error=str(error))
            except Exception as error:  # never let a registry bug wedge the run
                self.send_reply(call_id, False, error=f"{type(error).__name__}: {error}")

        threading.Thread(target=run, daemon=True).start()


class _AsyncTool:
    """Bridge for tools implemented as async callables (pwsh)."""

    def __init__(self, coroutine_fn) -> None:
        self._coroutine_fn = coroutine_fn
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def call(self, args: dict) -> Any:
        future = asyncio.run_coroutine_threadsafe(self._coroutine_fn(args), self._loop)
        return future.result()


_PWSH = _AsyncTool(_tool_pwsh)

REGISTRY: dict[str, Any] = {
    "pwsh": _PWSH,
    "read": _tool_read,
    "write": _tool_write,
    "edit": _tool_edit,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "web_fetch": _tool_web_fetch,
}


def _pump_channel(run: ProgramRun, channel_reader: io.BufferedReader) -> None:
    for line in channel_reader:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line.decode("utf-8"))
        except Exception:
            continue
        if not isinstance(frame, dict):
            continue
        kind = frame.get("type")
        if kind == "call":
            run.dispatch(frame.get("id"), str(frame.get("name")), frame.get("args"))
        elif kind == "done":
            run.done = frame
            run.done_event.set()
            return


def _pump_output(run: ProgramRun, pipe) -> None:
    for raw in iter(pipe.readline, b""):
        try:
            run.append_log(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        except Exception:
            continue
        if run.done_event.is_set():
            return


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        else:
            process.kill()
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def execute_program(code: str) -> dict:
    """Run one program in a fresh CPython process.

    Returns the DSH canonical run_code output: {logs, result?, sandbox}.
    Raises CodeRunFailed on program failure.
    """
    run = ProgramRun()
    parent_sock, child_sock = socket.socketpair()
    spawn_env = {
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "PYTHONUTF8": "1",
    }
    # Hand the child its socket end. Windows: subprocess pass_fds is unsupported,
    # so the socket travels as a WSADuplicateSocket blob (socket.share/fromshare)
    # inside the trusted stdin request, after the child exists (share() needs the
    # target pid). POSIX: the fd rides pass_fds + environment set before spawn.
    import base64

    channel_share: str | None = None
    pass_fds: tuple[int, ...] = ()
    if os.name == "nt":
        spawn_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")  # keep Python on Windows functional
    else:
        child_fd = child_sock.fileno()
        os.set_inheritable(child_fd, True)
        spawn_env["PTC_CHANNEL_FD"] = str(child_fd)
        pass_fds = (child_fd,)
    bootstrap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ptc_bootstrap.py")
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-I", bootstrap],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.getcwd(),
        env=spawn_env,
        pass_fds=pass_fds,
    )
    run.process = process
    if os.name == "nt":
        channel_share = base64.b64encode(child_sock.share(process.pid)).decode("ascii")
        child_sock.close()  # the child owns the duplicated handle now
    else:
        child_sock.close()  # the child owns the inherited fd now
    channel_reader = parent_sock.makefile("rb", buffering=0)
    run.reply_file = parent_sock.makefile("wb", buffering=0)
    try:
        assert process.stdin is not None
        request = json.dumps(
            {"program": code, **({"channel_share": channel_share} if channel_share else {})},
            ensure_ascii=False,
        ).encode("utf-8") + b"\n"
        process.stdin.write(request)
        process.stdin.close()
        threads = [
            threading.Thread(target=_pump_channel, args=(run, channel_reader), daemon=True),
            threading.Thread(target=_pump_output, args=(run, process.stdout), daemon=True),
            threading.Thread(target=_pump_output, args=(run, process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()
        settle_deadline_reached = not run.done_event.wait(MAX_WALL_MS / 1000.0)
        # Let the pump threads finish reading before tearing the pipes down.
        for thread in threads:
            thread.join(timeout=2.0)
        if settle_deadline_reached:
            _kill_tree(process)
            run.done = {
                "type": "done",
                "error": {
                    "kind": "timeout",
                    "message": f"the run exceeded the {MAX_WALL_MS} ms wall deadline",
                },
            }
        else:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_tree(process)
        sandbox = {"mode": STANDING_SANDBOX_MODE, "denied": False, "enforcement": SANDBOX_ENFORCEMENT}
        done = run.done
        assert done is not None
        error = done.get("error")
        if isinstance(error, dict):
            kind = str(error.get("kind") or "worker-exit")
            message = str(error.get("message") or "the run failed")
            raise CodeRunFailed(kind, message, run.logs, sandbox)
        if error is not None:
            raise CodeRunFailed("worker-exit", "the interpreter settled with a malformed done frame", run.logs, sandbox)
        if process.returncode not in (0, None):
            raise CodeRunFailed(
                "worker-exit",
                f"the interpreter exited with code {process.returncode} before settling",
                run.logs,
                sandbox,
            )
        return {
            "logs": run.logs,
            **({"result": done["value"]} if done.get("value") is not None else {}),
            "sandbox": sandbox,
        }
    finally:
        if run.process is not None and run.process.poll() is None:
            _kill_tree(run.process)
        for stream in (channel_reader, run.reply_file, process.stdin, process.stdout, process.stderr, parent_sock):
            try:
                stream.close()
            except OSError:
                pass


# ---------------------------------------------------------------- DSH result rendering

def render_value(value: Any) -> str:
    # DSH renderValue: a string result is presented verbatim; everything else as JSON.
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_run_code_output(output: dict) -> str:
    """The run_code output render (tool-executor-ptc ptc.ts output.render)."""
    logs: list[str] = output.get("logs") or []
    rendered = render_value(output["result"]) if output.get("result") is not None else ""
    parts = [part for part in ("\n".join(logs), rendered) if part]
    sandbox = output.get("sandbox")
    if isinstance(sandbox, dict):
        if sandbox.get("enforcement") == "partial":
            parts.append("File sandbox enforcement is partial on this host.")
        if sandbox.get("denied"):
            parts.append(f"The {sandbox.get('mode')} file sandbox denied an operation.{tool_schemas.ESCALATION_GUIDANCE}")
    return "\n".join(parts) if parts else "(run_code completed with no output)"


# ---------------------------------------------------------------- MCP transport

def _validate_escalation(args: dict) -> None:
    # DSH validateEscalationArgs: a wider mode requires a non-empty justification.
    requested = args.get("sandbox_permissions")
    if requested is None:
        return
    if requested not in tool_schemas.ESCALATION_TARGETS:
        raise ValueError(
            f"invalid sandbox_permissions: expected one of {tool_schemas.ESCALATION_TARGETS}"
        )
    justification = args.get("justification")
    if not isinstance(justification, str) or justification.strip() == "":
        raise ValueError("sandbox_permissions requires a non-empty justification")


def _apply_escalation(args: dict, sandbox: dict) -> None:
    requested = args.get("sandbox_permissions")
    if requested is not None:
        # The standing mode is danger-full-access, so no request can widen it:
        # DSH grants an escalation only when it is wider than the effective mode.
        sandbox["mode"] = requested


def handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name != RUN_CODE_TOOL:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"unknown tool: {name}; this server exposes only run_code"}],
        }
    mode = read_mode()
    if mode != "ptc":
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "run_code requires Agent Mode PTC mode. Run /agent-mode:ptc first — "
                    "the DSH ptc preset mounts the PTC executor, and without it there is no run_code transport.",
                }
            ],
        }
    if not isinstance(arguments, dict):
        return {"isError": True, "content": [{"type": "text", "text": "run_code arguments must be an object"}]}
    code = arguments.get("code")
    description = arguments.get("description")
    if not isinstance(code, str):
        return {"isError": True, "content": [{"type": "text", "text": "invalid code: expected a string"}]}
    if not isinstance(description, str) or description.strip() == "":
        return {"isError": True, "content": [{"type": "text", "text": "invalid description: expected a non-empty string"}]}
    try:
        _validate_escalation(arguments)
    except ValueError as error:
        return {"isError": True, "content": [{"type": "text", "text": str(error)}]}
    sandbox = {"mode": STANDING_SANDBOX_MODE, "denied": False, "enforcement": SANDBOX_ENFORCEMENT}
    try:
        _apply_escalation(arguments, sandbox)
        output = execute_program(code)
        output["sandbox"] = sandbox
        return {"content": [{"type": "text", "text": render_run_code_output(output)}]}
    except CodeRunFailed as error:
        return {"isError": True, "content": [{"type": "text", "text": str(error)}]}
    except Exception as error:
        return {"isError": True, "content": [{"type": "text", "text": f"code run failed (worker-exit): {error}"}]}


def handle_request(method: str, params: dict) -> dict | None:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": RUN_CODE_TOOL,
                    "description": RUN_CODE_DESCRIPTION,
                    "inputSchema": RUN_CODE_INPUT_SCHEMA,
                }
            ]
        }
    if method == "tools/call":
        return handle_tools_call(params)
    if method == "ping":
        return {}
    return None  # unknown method


JSONRPC_PARSE_ERROR = -32700
JSONRPC_METHOD_NOT_FOUND = -32601


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except Exception:
            _write_response(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": JSONRPC_PARSE_ERROR, "message": "parse error"}})
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            continue
        if request_id is None:
            continue  # notification (e.g. notifications/initialized): no response
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        try:
            result = handle_request(str(method), params)
        except Exception as error:  # a bug must not kill the transport
            _write_response(
                stdout,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"{type(error).__name__}: {error}"}},
            )
            continue
        if result is None:
            _write_response(
                stdout,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": JSONRPC_METHOD_NOT_FOUND, "message": f"method not found: {method}"}},
            )
            continue
        _write_response(stdout, {"jsonrpc": "2.0", "id": request_id, "result": result})
    return 0


def _write_response(stdout: io.BufferedWriter, payload: dict) -> None:
    try:
        stdout.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
