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
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tool_schemas  # noqa: E402
import mcp_proxy  # noqa: E402

SERVER_NAME = "agent-mode"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

MAX_PARALLEL_SUB_CALLS = 10  # DSH tool-executor-ptc default maxParallelSubCalls


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


# DSH Python runtime budgets are CONFIG fields (maxWallMs etc.); expose them as
# environment overrides. Defaults raised beyond DSH's 600000 ms so long builds
# (large Rust CI probes etc.) fit within one run_code call.
MAX_WALL_MS = _env_int("AGENT_MODE_MAX_WALL_MS", 3600000)
MAX_VALUE_BYTES = 32768  # DSH Python runtime maxValueBytes default
MAX_LOG_BYTES = 65536  # DSH Python runtime maxLogBytes default
LOG_TRUNCATION_MARKER = "[dsh-ptc-runtime-python] log capture truncated at 65536 bytes"
SHELL_DEFAULT_TIMEOUT_MS = 120000
SHELL_MAX_TIMEOUT_MS = _env_int("AGENT_MODE_SHELL_MAX_TIMEOUT_MS", 3600000)
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

EXCLUSIVE_TOOLS = {"pwsh", "write", "edit", "ask_user_question", "todo_write", "job_kill"}  # mutating/user-blocking calls run alone, in submission order

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
    """Resolve the active Agent Mode; mirrors scripts/agent_mode_hook.py.

    Session-aware when the harness passes CLAUDE_SESSION_ID: the session copy
    written by the enforcement hook wins; otherwise the global/workspace files.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    override = os.environ.get("AGENT_MODE_STATE_FILE")
    candidates: list[str] = []
    if override:
        candidates.append(override)
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID")
    if sid:
        candidates.append(os.path.join(home, ".zcode", "agent-mode-sessions", f"{sid}.json"))
    candidates.append(os.path.join(home, ".zcode", "agent-mode.json"))
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


def _tool_pwsh(args: dict) -> dict:
    command = args.get("command")
    description = args.get("description")
    if not isinstance(command, str) or not command:
        raise ToolFailure("pwsh failed: invalid command: expected a non-empty string")
    if not isinstance(description, str) or description.strip() == "":
        raise ToolFailure("pwsh failed: invalid description: expected a non-empty string")
    workdir = args.get("workdir")
    cwd = _resolve(workdir) if isinstance(workdir, str) and workdir else os.getcwd()
    if not os.path.isdir(cwd):
        raise ToolFailure(f"pwsh failed: workdir does not exist: {cwd}")
    shell = shutil.which("pwsh") or "powershell.exe"
    run_in_background = bool(args.get("run_in_background", False))
    if run_in_background:
        job = _start_shell_job(command, description, cwd, shell)
        return {"kind": "background", "cwd": cwd, "jobId": job.id}
    timeout_ms = args.get("timeoutMs", SHELL_DEFAULT_TIMEOUT_MS)
    if not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0:
        timeout_ms = SHELL_DEFAULT_TIMEOUT_MS
    timeout_ms = min(timeout_ms, SHELL_MAX_TIMEOUT_MS)
    # Popen + bounded wait so a timeout can PROMOTE to a background job
    # (DSH tool-bash promoteOnTimeout: default true) instead of being killed.
    process = subprocess.Popen(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    job = _Job(kind="pwsh", label=description, process=process)
    _start_job_pumps(job)
    timed_out = not job.done_event.wait(timeout_ms / 1000.0)
    if timed_out:
        _register_job(job)
        partial = job.snapshot_output()[0]
        return {
            "kind": "promoted",
            "cwd": cwd,
            "jobId": job.id,
            "timeoutMs": int(timeout_ms),
            "output": partial,
        }
    exit_code = process.poll()
    stdout_text, stdout_truncated = _clip(job.stdout_text(), SHELL_MAX_OUTPUT_CHARS)
    stderr_text, stderr_truncated = _clip(job.stderr_text(), SHELL_MAX_OUTPUT_CHARS)
    return {
        "kind": "foreground",
        "cwd": cwd,
        "exitCode": exit_code if exit_code is not None else 1,
        "signal": None,
        "timedOut": False,
        "aborted": False,
        "stdout": {"text": stdout_text, "truncated": stdout_truncated},
        "stderr": {"text": stderr_text, "truncated": stderr_truncated},
    }


# ---------------------------------------------------------------- jobs (DSH tool-jobs)

JOB_WAIT_DEFAULT_MS = 30000
JOB_WAIT_MAX_MS = _env_int("AGENT_MODE_JOB_WAIT_MAX_MS", 3600000)


class _Job:
    def __init__(self, kind: str, label: str, process: subprocess.Popen) -> None:
        self.id = f"job-{next(_job_counter)}"
        self.kind = kind
        self.label = label
        self.process = process
        self.status = "running"
        self.detail: str | None = None
        self.started_at = int(time.time() * 1000)
        self.finished_at: int | None = None
        self.stdout_parts: list[str] = []
        self.stderr_parts: list[str] = []
        self.read_cursor = 0  # job_output reads output SINCE the previous read
        self.done_event = threading.Event()
        self._parts_lock = threading.Lock()
        self.active_pumps = 2  # stdout + stderr; settle only when BOTH drain

    def _append(self, sink: list[str], chunk: str) -> None:
        with self._parts_lock:
            sink.append(chunk)

    def pump(self, pipe, sink: list[str]) -> None:
        try:
            for raw in iter(pipe.readline, b""):
                self._append(sink, raw.decode("utf-8", errors="replace"))
        except Exception:
            pass
        finally:
            with self._parts_lock:
                self.active_pumps -= 1
                drained = self.active_pumps == 0
            if drained:
                self._settle()

    def _settle(self) -> None:
        if self.status == "running":
            try:
                code = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
            self.finished_at = int(time.time() * 1000)
            if code == 0:
                self.status = "completed"
            else:
                self.status = "failed"
                self.detail = f"exit code {code}"
        self.done_event.set()

    def snapshot_output(self) -> tuple[str, str]:
        with self._parts_lock:
            return "".join(self.stdout_parts), "".join(self.stderr_parts)

    def stdout_text(self) -> str:
        return self.snapshot_output()[0]

    def stderr_text(self) -> str:
        return self.snapshot_output()[1]

    def since_cursor(self) -> str:
        with self._parts_lock:
            joined = "".join(self.stdout_parts)
            tail = joined[self.read_cursor:]
            self.read_cursor = len(joined)
        return tail

    def public(self) -> dict:
        shape = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "startedAt": self.started_at,
        }
        if self.detail is not None:
            shape["detail"] = self.detail
        if self.finished_at is not None:
            shape["finishedAt"] = self.finished_at
        return shape


_job_counter = iter(range(1, 10**9))
_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()


def _start_job_pumps(job: _Job) -> None:
    assert job.process.stdout is not None and job.process.stderr is not None
    threading.Thread(target=job.pump, args=(job.process.stdout, job.stdout_parts), daemon=True).start()
    threading.Thread(target=job.pump, args=(job.process.stderr, job.stderr_parts), daemon=True).start()


def _start_shell_job(command: str, label: str, cwd: str, shell: str) -> _Job:
    process = subprocess.Popen(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    job = _Job(kind="pwsh", label=label, process=process)
    _start_job_pumps(job)
    _register_job(job)
    return job


def _register_job(job: _Job) -> None:
    with _JOBS_LOCK:
        _JOBS[job.id] = job


def _get_job(job_id: str) -> _Job:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise ToolFailure(f"job_output failed: unknown job id: {job_id}")
    return job


def _tool_job_output(args: dict) -> dict:
    job_id = args.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ToolFailure("job_output failed: invalid job_id: expected a non-empty string")
    job = _get_job(job_id)
    if args.get("wait"):
        timeout_ms = args.get("timeout_ms", JOB_WAIT_DEFAULT_MS)
        if not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0:
            timeout_ms = JOB_WAIT_DEFAULT_MS
        timeout_ms = min(timeout_ms, JOB_WAIT_MAX_MS)
        job.done_event.wait(timeout_ms / 1000.0)
        # DSH: a timed-out wait returns job state, not a timeout error.
    text = job.since_cursor()
    return {"text": text if text else "(no new output)", "job": job.public()}


def _tool_job_list(args: dict) -> list:
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    return [job.public() for job in jobs]


def _tool_job_kill(args: dict) -> dict:
    job_id = args.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ToolFailure("job_kill failed: invalid job_id: expected a non-empty string")
    job = _get_job(job_id)
    if job.status == "running":
        try:
            _kill_process_tree(job.process)
        except OSError:
            pass
        job.status = "killed"
        job.finished_at = int(time.time() * 1000)
        job.done_event.set()
        return {"outcome": "cancellation-requested", "job": job.public()}
    return {"outcome": "already-finished", "job": job.public()}


def _kill_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()


# ---------------------------------------------------------------- skill (DSH tool-skill)

SKILL_SEARCH_DIRS = (
    ("user", (".zcode", "skills"), 400),
    ("user-agents", (".agents", "skills"), 500),
)


def _tool_skill(args: dict) -> dict:
    name = args.get("name")
    if not isinstance(name, str) or not name:
        raise ToolFailure('skill failed: expected the exact skill name from the available skills list')
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    roots = [
        os.path.join(home, ".zcode", "skills"),
        os.path.join(home, ".agents", "skills"),
        os.path.join(os.getcwd(), ".zcode", "skills"),
        os.path.join(os.getcwd(), ".agents", "skills"),
    ]
    candidates = [os.path.join(root, name, "SKILL.md") for root in roots]
    candidates += [os.path.join(root, f"{name}.md") for root in roots]
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            with io.open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                body = handle.read()
        except OSError as error:
            raise ToolFailure(f"skill failed: {error}") from None
        content = _strip_frontmatter(body)
        return {"name": name, "provider": "filesystem", "content": content}
    raise ToolFailure(f'skill "{name}" is unknown or no longer available')


def _strip_frontmatter(body: str) -> str:
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            return body[end + 4:].lstrip("\n").strip()
    return body.strip()


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


# ---------------------------------------------------------------- client requests (elicitation)

_pending_client_responses: dict[int, dict] = {}
_pending_events: dict[int, threading.Event] = {}
_client_request_id_lock = threading.Lock()
_client_request_next_id = 0
STDOUT_LOCK = threading.Lock()
CLIENT_SUPPORTS_ELICITATION = False
CLIENT_SUPPORTS_SAMPLING = False

ELICITATION_CALL_TIMEOUT_S = 600

# DSH timed-mode pending message (packages/interaction/tool-ask-user/src/timed.ts),
# used verbatim when the client cannot elicit and the binding degrades to pending.
DSH_PENDING_MESSAGE = (
    "No answer batch arrived before the timeout. This is pending, not a skipped answer. "
    "Continue useful independent work. The user can still answer; their reply will be a "
    "user message identified as answer_to_pending_question with this callId and the "
    "original questions. Do not treat this as permission."
)


def send_client_request(method: str, params: dict, timeout_s: float) -> dict:
    """Send a server→client JSON-RPC request and wait for its response.

    The reader loop resolves the future via _on_client_response.
    """
    global _client_request_next_id
    with _client_request_id_lock:
        _client_request_next_id += 1
        request_id = _client_request_next_id
    event = threading.Event()
    with STDOUT_LOCK:
        _pending_events[request_id] = event
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    with STDOUT_LOCK:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except OSError as error:
            _pending_events.pop(request_id, None)
            raise RuntimeError(f"transport write failed: {error}") from None
    if not event.wait(timeout_s):
        _pending_events.pop(request_id, None)
        raise RuntimeError(f"no response to {method} within {timeout_s:g}s")
    with STDOUT_LOCK:
        _pending_events.pop(request_id, None)
        response = _pending_client_responses.pop(request_id, None)
    if response is None:
        raise RuntimeError(f"no response to {method}")
    if "error" in response:
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"{method} rejected: {message}")
    return response.get("result") or {}


def _on_client_response(request_id: int, message: dict) -> None:
    with STDOUT_LOCK:
        event = _pending_events.get(request_id)
        if event is not None:
            _pending_client_responses[request_id] = message
            event.set()


def _format_questions_for_elicitation(questions: list[dict]) -> tuple[str, dict]:
    """Render DSH's legacy question batch as one elicitation message + schema."""
    lines: list[str] = []
    schema_properties: dict = {}
    required: list[str] = []
    for question in questions:
        qid = str(question.get("id"))
        text = str(question.get("question"))
        header = question.get("header")
        options = question.get("options") or []
        multi = bool(question.get("multi_select", False))
        title = f"{header}: {text}" if isinstance(header, str) and header else text
        block = title
        if options:
            labels = [str(o.get("label")) for o in options if isinstance(o, dict) and o.get("label")]
            descriptions = {
                str(o.get("label")): str(o.get("description"))
                for o in options
                if isinstance(o, dict) and o.get("label") and o.get("description")
            }
            if labels:
                block += " Options:"
                for label in labels:
                    description = descriptions.get(label)
                    block += f"\n  - {label}" + (f" — {description}" if description else "")
                block += "\nAnswer with the option label" + ("s, comma-separated" if multi else "") + "; or type your own."
        schema_properties[qid] = {
            "type": "string",
            "description": "Your answer" + (" (option labels, comma-separated, or free text)" if options else ""),
        }
        required.append(qid)
        lines.append(block)
    message = "\n\n".join(lines)
    schema = {"type": "object", "properties": schema_properties, "required": required}
    return message, schema


def _parse_answer_text(text: str, question: dict) -> dict:
    """Map the elicited free text onto DSH's answer shape {id, selected, custom?}."""
    options = question.get("options") or []
    labels = [str(o.get("label")) for o in options if isinstance(o, dict) and o.get("label")]
    by_lower = {label.lower(): label for label in labels}
    selected: list[str] = []
    custom_parts: list[str] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        matched = by_lower.get(token.lower())
        if matched is not None:
            selected.append(matched)
        else:
            custom_parts.append(token)
    answer: dict = {"id": str(question.get("id")), "selected": selected}
    if custom_parts:
        answer["custom"] = "; ".join(custom_parts)
    return answer


def _tool_ask_user_question(args: dict) -> dict:
    questions = args.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ToolFailure("ask_user_question failed: questions must be a non-empty array")
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("id"), str) or not isinstance(question.get("question"), str):
            raise ToolFailure("ask_user_question failed: each question needs string id and question")
    call_id = f"ask-{os.getpid()}-{int(time.time() * 1000)}"
    if CLIENT_SUPPORTS_ELICITATION:
        try:
            message, schema = _format_questions_for_elicitation(questions)
            result = send_client_request(
                "elicitation/create",
                {"message": message, "requestedSchema": schema},
                timeout_s=ELICITATION_CALL_TIMEOUT_S,
            )
            action = result.get("action")
            content = result.get("content") or {}
            if action == "accept":
                answers = []
                for question in questions:
                    qid = str(question.get("id"))
                    text = content.get(qid) if isinstance(content, dict) else None
                    if isinstance(text, str) and text.strip():
                        answers.append(_parse_answer_text(text, question))
                    else:
                        answers.append({"id": qid, "selected": []})  # DSH skip semantics
                return {"answers": answers}
            # decline/cancel: DSH skip semantics — empty selected, no custom.
            return {"answers": [{"id": str(q.get("id")), "selected": []} for q in questions]}
        except RuntimeError:
            pass  # fall through to the DSH timed-mode pending shape
    # Client cannot elicit: degrade to DSH's timed-mode pending result. The
    # answer will arrive as a later user message in the conversation.
    return {
        "pending": True,
        "callId": call_id,
        "message": DSH_PENDING_MESSAGE,
    }


def _todos_state_path() -> str:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".zcode", "agent-mode-todos.json")


def _tool_todo_write(args: dict) -> dict:
    todos = args.get("todos")
    if not isinstance(todos, list):
        raise ToolFailure("todo_write failed: todos must be an array")
    valid_statuses = {"pending", "in_progress", "completed"}
    clean: list[dict] = []
    for todo in todos:
        if not isinstance(todo, dict) or not isinstance(todo.get("content"), str) or todo.get("status") not in valid_statuses:
            raise ToolFailure(
                "invalid todos: each todo needs a string content and a status in pending|in_progress|completed"
            )
        clean.append({"content": todo["content"], "status": todo["status"]})
    counts = {
        "pending": sum(1 for t in clean if t["status"] == "pending"),
        "inProgress": sum(1 for t in clean if t["status"] == "in_progress"),
        "completed": sum(1 for t in clean if t["status"] == "completed"),
    }
    try:
        with io.open(_todos_state_path(), "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"todos": clean}, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass  # the durable copy is best-effort; the canonical value still returns
    return {"todos": clean, "counts": counts}


REGISTRY = {
    "pwsh": _tool_pwsh,
    "read": _tool_read,
    "write": _tool_write,
    "edit": _tool_edit,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "web_fetch": _tool_web_fetch,
    "ask_user_question": _tool_ask_user_question,
    "todo_write": _tool_todo_write,
    "job_output": _tool_job_output,
    "job_list": _tool_job_list,
    "job_kill": _tool_job_kill,
    "skill": _tool_skill,
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
                call_args = args if isinstance(args, dict) else {}
                tool = REGISTRY.get(name)
                if tool is not None:
                    # DSH native concurrency contract: mutating calls run alone, in
                    # submission order; safe calls may overlap, capped at maxParallel.
                    if name in EXCLUSIVE_TOOLS:
                        with self.exclusive_lock:
                            with self.parallel_semaphore:
                                value = tool(call_args)
                    else:
                        with self.parallel_semaphore:
                            value = tool(call_args)
                elif name in mcp_proxy.PROXY.tools:
                    # Fail-closed exclusive (DSH's classification for undeclared
                    # execution modes): proxied MCP calls run alone, in order.
                    with self.exclusive_lock:
                        with self.parallel_semaphore:
                            value = mcp_proxy.PROXY.call(name, call_args)
                else:
                    raise ToolFailure(
                        f"unknown tool binding: {name} — it is not part of the PTC registry "
                        "(the built-in SDK plus this session's projected MCP bindings)"
                    )
                self.send_reply(call_id, True, value=value)
            except ToolFailure as error:
                self.send_reply(call_id, False, error=str(error))
            except Exception as error:  # never let a registry bug wedge the run
                self.send_reply(call_id, False, error=f"{type(error).__name__}: {error}")

        threading.Thread(target=run, daemon=True).start()


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
    # Project the session's MCP servers into bindings before the program runs
    # (DSH binds every agent-visible tool, MCP tools included). A discovery
    # failure must never fail the program: the registry just stays smaller.
    try:
        mcp_proxy.PROXY.discover()
    except Exception as error:
        mcp_proxy.PROXY.notes.append(mcp_proxy.ProxyNote("(discovery)", f"{type(error).__name__}: {error}"))
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
    global CLIENT_SUPPORTS_ELICITATION, CLIENT_SUPPORTS_SAMPLING
    if method == "initialize":
        capabilities = params.get("capabilities") or {}
        CLIENT_SUPPORTS_ELICITATION = isinstance(capabilities, dict) and "elicitation" in capabilities
        CLIENT_SUPPORTS_SAMPLING = isinstance(capabilities, dict) and "sampling" in capabilities
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

_ROOT_RUN_LOCK = threading.Lock()  # DSH: root run_code calls are exclusive, serial


def _handle_client_request(method: str, params: dict, request_id: Any) -> None:
    """Serve one client→server request (tools/call, ping) on its own thread."""
    try:
        if method == "tools/call" and params.get("name") == RUN_CODE_TOOL:
            with _ROOT_RUN_LOCK:  # DSH: root run_code calls are exclusive and execute serially
                result = handle_request(str(method), params)
        else:
            result = handle_request(str(method), params)
    except Exception as error:  # a bug must not kill the transport
        _write_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"{type(error).__name__}: {error}"}})
        return
    if result is None:
        _write_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": JSONRPC_METHOD_NOT_FOUND, "message": f"method not found: {method}"}})
    else:
        _write_message({"jsonrpc": "2.0", "id": request_id, "result": result})


def _write_message(payload: dict) -> None:
    with STDOUT_LOCK:
        try:
            sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
        except OSError:
            pass


def _read_loop() -> None:
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except Exception:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            # A response to one of OUR requests (e.g. elicitation/create).
            if request_id is not None:
                try:
                    _on_client_response(int(request_id), message)
                except (TypeError, ValueError):
                    pass
            continue
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if request_id is None:
            continue  # notification (e.g. notifications/initialized)
        threading.Thread(
            target=_handle_client_request, args=(str(method), params, request_id), daemon=True
        ).start()


def main() -> int:
    reader = threading.Thread(target=_read_loop, daemon=True)
    reader.start()
    try:
        while reader.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


def _write_response(stdout: io.BufferedWriter, payload: dict) -> None:
    try:
        stdout.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
