# -*- coding: utf-8 -*-
"""DSH PTC tool contracts for the Agent Mode port.

Every description string below is copied VERBATIM from the DeepSeek Harness
sources (packages/shell/tool-pwsh, packages/fs/tool-fs, packages/fs/tool-fs-search,
packages/web/tool-web) so the generated SDK the model programs against is
byte-faithful to the DSH `ptc` agent preset. Output schemas declare the same
canonical shapes, limited to the arms this port can actually produce.
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS: list[dict] = []
"""ToolSdkSchema list: {name, description, parameters, output} (JSON Schema)."""

BASH_LIKE_DESCRIPTION_PARAM = (
    "Clear, concise description of what this command does in active voice, 5-10 words "
    '(shown in the UI). Examples: "ls" → "List files in current directory"; '
    '"git status" → "Show working tree status"; "npm install" → "Install package dependencies".'
)

PWSH_DESCRIPTION = (
    "Execute a PowerShell command (`pwsh -Command`) and return its stdout/stderr. "
    "Each call runs in a fresh pwsh process; pass `workdir` instead of using `cd`. "
    "Paths use native Windows form (`C:\\...`); read environment variables with `$env:NAME`. "
    "Managed `$env:DSH_*` variables expose current harness environment facts. "
    "Long output is truncated to its tail; the full output is saved to a file whose path is reported when available. "
    "On Windows a force-killed command settles as `[exit code: 1]` without a signal marker — treat it as an interruption, not a command failure. "
    "Provide `description` before `command` in the arguments. "
    "Before any delete or move, verify that the resolved absolute target path is the intended one; never run it against a computed path you have not checked. "
    "Do not assign to automatic variables such as `$HOME`; variable names are case-insensitive, so `$home` is the same read-only variable. "
    "Commands may run under a file sandbox; a blocked file operation is reported as "
    "`[sandbox: file access denied under <mode> mode]`, a policy denial: do not retry another way."
)

READ_DESCRIPTION = "Read a UTF-8 text file and return line-numbered content."

WRITE_DESCRIPTION = "Create or fully replace a UTF-8 text file."

EDIT_DESCRIPTION = "Edit an existing UTF-8 text file by replacing literal text."

GLOB_DESCRIPTION = (
    "Find files, not directories, whose paths match a glob pattern, including hidden and ignored files. "
    "Returns up to 100 paths in modification-time order; a larger result keeps the first paths and reports "
    "where the complete list was saved."
)

GREP_DESCRIPTION = (
    "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, "
    "grouped by file. Returns up to 250 matches; a larger result reports where the complete match list was saved."
)

WEB_FETCH_DESCRIPTION = "Fetch the content of a specific HTTP(S) URL and return it decoded to text."

SHELL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "const": "foreground"},
        "cwd": {"type": "string"},
        "exitCode": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
        "signal": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "timedOut": {"type": "boolean"},
        "aborted": {"type": "boolean"},
        "stdout": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "truncated": {"type": "boolean"}},
            "required": ["text", "truncated"],
        },
        "stderr": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "truncated": {"type": "boolean"}},
            "required": ["text", "truncated"],
        },
    },
    "required": ["kind", "cwd", "exitCode", "signal", "timedOut", "aborted", "stdout", "stderr"],
}

TOOLS = [
    {
        "name": "pwsh",
        "description": PWSH_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The PowerShell command to execute."},
                "description": {"type": "string", "description": BASH_LIKE_DESCRIPTION_PARAM},
                "timeoutMs": {
                    "type": "number",
                    "description": "Timeout in milliseconds. The executor applies its configured default and cap, and kills the command on expiry.",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory for this command. Defaults to the session workspace; a relative path is resolved against it.",
                },
            },
            "required": ["command", "description"],
        },
        "output": SHELL_OUTPUT_SCHEMA,
    },
    {
        "name": "read",
        "description": READ_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to read, resolved by the filesystem backend."},
                "offset": {"type": "number", "description": "1-based first line to return. Defaults to 1."},
                "limit": {"type": "number", "description": "Maximum number of lines to return. Defaults to 2000."},
            },
            "required": ["file_path"],
        },
        "output": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"number": {"type": "integer"}, "text": {"type": "string"}},
                        "required": ["number", "text"],
                    },
                },
                "totalLines": {"type": "integer"},
            },
            "required": ["path", "offset", "lines", "totalLines"],
        },
    },
    {
        "name": "write",
        "description": WRITE_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to write, resolved by the filesystem backend. Provide `file_path` before `content` in the arguments.",
                },
                "content": {"type": "string", "description": "Full UTF-8 text content to write."},
            },
            "required": ["file_path", "content"],
        },
        "output": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "operation": {"type": "string", "enum": ["create", "update"]},
                "before": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                "after": {"type": "string"},
            },
            "required": ["path", "operation", "before", "after"],
        },
    },
    {
        "name": "edit",
        "description": EDIT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to edit, resolved by the filesystem backend. Provide `file_path` before `old_string` and `new_string` in the arguments.",
                },
                "old_string": {"type": "string", "description": "Literal text to replace."},
                "new_string": {
                    "type": "string",
                    "description": "Literal replacement text. Use an empty string to delete the match.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all matches. Defaults to false; when false, old_string must appear exactly once.",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        "output": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "before": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                "after": {"oneOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["path", "before", "after"],
        },
    },
    {
        "name": "glob",
        "description": GLOB_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": 'Glob pattern to match file paths against (e.g. "**/*.ts", "src/**/*.test.js"). '
                    'A pattern with no "/" matches the basename at any depth, so "*" and "*.ts" both search the whole tree; '
                    "include a separator to anchor the depth.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in. Defaults to the session workspace; a relative path resolves against it.",
                },
            },
            "required": ["pattern"],
        },
        "output": {
            "type": "object",
            "properties": {"root": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["root", "paths"],
        },
    },
    {
        "name": "grep",
        "description": GREP_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for (ripgrep syntax)."},
                "path": {
                    "type": "string",
                    "description": "File or directory to search. Defaults to the session workspace; a relative path resolves against it.",
                },
                "include": {
                    "type": "string",
                    "description": 'One glob filter for which files to search (e.g. "*.ts", "*.{js,jsx}"). Not a list; negation is not supported.',
                },
            },
            "required": ["pattern"],
        },
        "output": {
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "lineNumber": {"type": "integer"},
                            "line": {"type": "string"},
                        },
                        "required": ["path", "lineNumber", "line"],
                    },
                },
            },
            "required": ["matches"],
        },
    },
    {
        "name": "web_fetch",
        "description": WEB_FETCH_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The HTTP(S) URL to fetch."},
            },
            "required": ["url"],
        },
        "output": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "statusCode": {"type": "integer"},
                "body": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["html", "text"]},
                        "content": {"type": "string"},
                    },
                    "required": ["kind", "content"],
                },
                "truncated": {"type": "boolean"},
            },
            "required": ["url", "statusCode", "body", "truncated"],
        },
    },
]

# --- run_code transport (packages/core/tool-executor-ptc/src/ptc.ts, python flavor) ---

RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION = (
    "Clear, concise description of what this program does in active voice, "
    "5-10 words (shown in the UI). "
    'Examples: "Count TODO markers across packages"; '
    '"Read failing test and its fixture"; "Rename config key in every cordis.yml".'
)

PYTHON_RUNTIME_EXECUTION_INSTRUCTIONS = (
    "The program is the body of an async Python function. Top-level await and return are supported. "
    "Emit output with print(...) or return a lossless JSON value. Only printed values and the returned "
    "value become program output. Each call runs in a fresh Python process; variables and imported "
    "modules do not persist across calls. Relative paths use the supplied working directory; only TMPDIR "
    "is set in the environment. Direct filesystem access follows this execution's sandbox policy."
)

ESCALATION_GUIDANCE = (
    " A sandbox escalation approves this complete program for one execution only. Nested tools retain "
    "their own policies and approvals. Request wider access only after evidence of a denial. Earlier "
    "effects may already have completed: inspect them before explicitly retrying. Programs are never "
    "replayed automatically."
)

ESCALATION_TARGETS = ["workspace-write", "danger-full-access"]

SANDBOX_PERMISSIONS_PARAM_DESCRIPTION = (
    "Wider sandbox mode for this complete program execution; requires justification and approval."
)

JUSTIFICATION_PARAM_DESCRIPTION = (
    "Reason this complete program needs wider access, shown to the user for approval. "
    "Use the language of the user\u2019s current request."
)


def build_run_code_description() -> str:
    """Assemble run_code's model-facing description exactly as ptc.ts does for the Python flavor."""
    base = (
        "Execute a Python program against the available tools. Takes two required arguments: "
        "`code`, the program to execute, and `description`, a short summary of what the program does. "
        "Call tools as `await tools.name(args)` per the declarations in the system prompt. "
        "Curate the program output. Image-bearing subtool results are attached after the run."
    )
    return (
        base
        + " "
        + PYTHON_RUNTIME_EXECUTION_INSTRUCTIONS
        + " Each program starts in the Session's current directory. Running programs keep their initial directory."
        + ESCALATION_GUIDANCE
    )


def build_run_code_input_schema() -> dict:
    """The run_code input schema for a runtime with no timeout overrides and an advertised sandbox."""
    return {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python program to execute, following this tool's execution instructions.",
            },
            "description": {"type": "string", "description": RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION},
            "sandbox_permissions": {
                "type": "string",
                "enum": list(ESCALATION_TARGETS),
                "description": SANDBOX_PERMISSIONS_PARAM_DESCRIPTION,
            },
            "justification": {"type": "string", "description": JUSTIFICATION_PARAM_DESCRIPTION},
        },
        "required": ["code", "description"],
    }
