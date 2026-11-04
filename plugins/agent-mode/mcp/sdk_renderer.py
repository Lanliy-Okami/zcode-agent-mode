# -*- coding: utf-8 -*-
"""Faithful Python port of DSH's renderToolsSdkPy (packages/core/tool-executor-ptc/src/py-types.ts).

Rendered under ``runtime.language === 'python'``: the fixed usage instructions plus one
named ``TypedDict`` per tool argument/output object (and per nested object) and one
awaitable method per visible tool on a ``Tools`` protocol. Deterministic: tools in
lexicographic name order, class declarations precede the protocol in that same order
(nested classes before the parent that references them).

Run as a script to regenerate the ``tools:sdk`` section for the current tool set:
    python -X utf8 sdk_renderer.py
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from typing import Any

import tool_schemas

# py-types.ts IDENTIFIER: XID_Start XID_Continue* — str.isidentifier() accepts exactly
# that set on the CPython actually running this code, which is a strictly better match
# than the TS-side approximation (the version-skew concern exists only across engines).
def is_bare_identifier(name: str) -> bool:
    return name.isidentifier() and unicodedata.normalize("NFKC", name) == name


# py-types.ts RESERVED: Python hard keywords plus __debug__ (cannot be assigned at
# compile time). Soft keywords are deliberately absent.
RESERVED = {
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
    "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
    "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
    "return", "try", "while", "with", "yield",
    "__debug__",
}

TYPING_ORDER = ["Any", "Literal", "NotRequired", "Protocol", "TypedDict"]

MAX_CLASS_NAME_BASE = 120
MAX_LIST_NESTING = 180

# py-types.ts UNPRINTABLE: C0 controls (minus those \s already collapses), DEL, C1 controls.
UNPRINTABLE = re.compile(r"[\u0000-\u0008\u000e-\u001f\u007f-\u009f]")
# py-types.ts LONE_SURROGATE: unpaired surrogate code points.
LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def pad(indent: int) -> str:
    return "    " * indent


def describe(schema: dict) -> str | None:
    """Collapsed one-line ``description`` of a schema node, or None when absent/empty."""
    description = schema.get("description") if isinstance(schema, dict) else None
    if not isinstance(description, str):
        return None
    collapsed = re.sub(r"\s+", " ", description)
    collapsed = UNPRINTABLE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", collapsed)
    collapsed = LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", collapsed)
    collapsed = collapsed.strip()
    return collapsed if collapsed else None


def doc_lines(description: Any, indent: int) -> list[str]:
    collapsed = describe({"description": description} if isinstance(description, str) else {})
    if collapsed is None:
        return []
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return [f"{pad(indent)}\"\"\"{escaped}\"\"\""]


def camel_case(raw: str) -> str:
    """camelCase a name into a Python type identifier (py-types.ts camelCase)."""
    parts = [p for p in re.split(r"[^\w]+|_+", raw) if p]
    joined = "".join(p[0].upper() + p[1:] for p in parts)
    joined = unicodedata.normalize("NFKC", joined)
    # \p{XID_Start} ≈ letters + underscore (never digits); [^\W\d] matches that set.
    if not re.match(r"[^\W\d]", joined, re.UNICODE):
        joined = unicodedata.normalize("NFKC", f"Tool{joined}")
    return joined


def cap_class_name_base(base: str) -> str:
    if len(base) <= MAX_CLASS_NAME_BASE:
        return base
    return base[:MAX_CLASS_NAME_BASE]


class RenderState:
    def __init__(self) -> None:
        self.classes: list[str] = []
        self.used_class_names: set[str] = set()
        self.next_class_counter: dict[str, int] = {}
        self.typing: set[str] = {"Protocol"}

    def allocate_class_name(self, base: str) -> str:
        capped = cap_class_name_base(base)
        name = capped
        if name in self.used_class_names:
            n = self.next_class_counter.get(capped, 2)
            while f"{capped}{n}" in self.used_class_names:
                n += 1
            name = f"{capped}{n}"
            self.next_class_counter[capped] = n + 1
        self.used_class_names.add(name)
        return name

    def child_class_name(self, base: str, segment: str) -> str:
        return cap_class_name_base(unicodedata.normalize("NFKC", base + segment))


def py_scalar(value: Any) -> str:
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        return json.dumps(value)
    return str(value)


def render_constrained_scalar(node: dict, broad: str, state: RenderState) -> str:
    if "const" in node:
        state.typing.add("Literal")
        return f"Literal[{py_scalar(node['const'])}]"
    if "enum" in node:
        state.typing.add("Literal")
        return "Literal[" + ", ".join(py_scalar(v) for v in node["enum"]) + "]"
    return broad


def render_type(schema: Any, class_name: str, state: RenderState) -> str:
    """Map one JSON-Schema node to a Python type expression (py-types.ts renderType)."""
    try:
        if not isinstance(schema, dict):
            raise ValueError()
        result: str | None = None

        def walk(node: dict, name: str, list_depth: int) -> str:
            if "oneOf" in node:
                branches = [walk(b, state.child_class_name(name, str(i + 1)), list_depth)
                            for i, b in enumerate(node["oneOf"])]
                return " | ".join(branches)
            node_type = node.get("type")
            if node_type is None:
                state.typing.add("Any")
                return "Any"
            if node_type == "string":
                return render_constrained_scalar(node, "str", state)
            if node_type == "number":
                return render_constrained_scalar(node, "float", state)
            if node_type == "integer":
                return render_constrained_scalar(node, "int", state)
            if node_type == "boolean":
                return render_constrained_scalar(node, "bool", state)
            if node_type == "null":
                return "None"
            if node_type == "array":
                items = node.get("items")
                if items is None:
                    state.typing.add("Any")
                    return "list[Any]"
                if list_depth >= MAX_LIST_NESTING:
                    state.typing.add("Any")
                    return "Any"
                return f"list[{walk(items, name, list_depth + 1)}]"
            if node_type == "object":
                entries = list((node.get("properties") or {}).items())
                if (
                    name == ""
                    or not all(
                        is_bare_identifier(field)
                        and field not in RESERVED
                        and not (field.startswith("__") and not field.endswith("__"))
                        for field, _ in entries
                    )
                ):
                    state.typing.add("Any")
                    return "dict[str, Any]"
                if len(entries) == 0 and node.get("additionalProperties") is not False:
                    state.typing.add("Any")
                    return "dict[str, Any]"
                allocated = state.allocate_class_name(name)
                state.typing.add("TypedDict")
                required = set(node.get("required") or [])
                lines = [f"class {allocated}(TypedDict):"]
                for field, child in entries:
                    field_type = walk(child, state.child_class_name(allocated, camel_case(field)), 1)
                    description = describe(child)
                    if description is not None:
                        lines.append(f"{pad(1)}# {description}")
                    if field in required:
                        lines.append(f"{pad(1)}{field}: {field_type}")
                    else:
                        state.typing.add("NotRequired")
                        lines.append(f"{pad(1)}{field}: NotRequired[{field_type}]")
                if node.get("additionalProperties") is not False:
                    lines.append(f"{pad(1)}# Additional keys beyond those declared are allowed.")
                if len(lines) == 1:
                    lines.append(f"{pad(1)}pass")
                state.classes.append("\n".join(lines))
                return allocated
            state.typing.add("Any")
            return "Any"

        result = walk(schema, class_name, 0)
        return result if result is not None else "Any"
    except Exception:
        state.typing.add("Any")
        return "Any"


SDK_INSTRUCTIONS = (
    "## Writing code for run_code\n"
    "\n"
    "`run_code` takes two required arguments: `code`, the program to execute, and `description`, "
    "a short summary of what the program does. At run time exactly two of the names declared below "
    "are bound: `tools` and `ToolCallError`. Everything else is a STATIC STUB describing argument and "
    "return types — in particular the `TypedDict` classes do NOT exist at run time, so build arguments "
    "as plain `dict`/`list` JSON values: `await tools.name({\"field\": 1})`, never `FooArgs(field=1)`, "
    "which raises `NameError`. Inside the program:\n"
    "\n"
    "- Call tools as `await tools.name(args)` — subscript access for exotic, reserved, or "
    'underscore-leading names: `await tools["my-tool"](args)`. Every call resolves to the tool\'s typed '
    "canonical JSON value (each method's return type below). Tool arguments must be lossless JSON.\n"
    "- A FAILED tool call raises `ToolCallError`, whose `toolName` identifies the failed tool and whose "
    "message is human-readable — wrap in `try/except` to handle and continue.\n"
    "- Independent read-only calls MAY overlap under `asyncio.gather` (safe calls run concurrently; "
    "mutating calls run alone, in submission order). Sequence dependent work with `await`.\n"
    "- Only values emitted as program output enter the conversation. A successful tool result containing "
    "an image is attached after the run so you can inspect it on the next step; every other intermediate "
    "result stays out of the conversation, so extract just what you need.\n"
    "\n"
    "The available tools:"
)


def render_tools_sdk_py(schemas: list[dict]) -> str:
    """The complete ``tools:sdk`` section text (py-types.ts renderToolsSdkPy)."""
    sorted_schemas = sorted(schemas, key=lambda s: s["name"])
    state = RenderState()
    members: list[str] = []
    statements = 0
    for schema in sorted_schemas:
        arg_type = render_type(schema["parameters"], f"{camel_case(schema['name'])}Args", state)
        output_type = render_type(schema["output"], f"{camel_case(schema['name'])}Output", state)
        if is_bare_identifier(schema["name"]) and schema["name"] not in RESERVED and not schema["name"].startswith("_"):
            doc = doc_lines(schema.get("description"), 2)
            members.append(
                f"{pad(1)}async def {schema['name']}(self, args: {arg_type}) -> {output_type}:" if doc
                else f"{pad(1)}async def {schema['name']}(self, args: {arg_type}) -> {output_type}: ..."
            )
            members.extend(doc)
            statements += 1
        else:
            members.append(
                f"{pad(1)}# tools[{json.dumps(schema['name'])}](args: {arg_type}) -> {output_type}"
            )
            description = describe(schema)
            if description is not None:
                members.append(f"{pad(1)}#   {description}")
    body_lines = members if statements > 0 else [f"{pad(1)}pass", *members]
    body = "\n".join(body_lines)
    imports = [symbol for symbol in TYPING_ORDER if symbol in state.typing]
    class_block = f"{chr(10).join(state.classes)}{chr(10)}{chr(10)}" if state.classes else ""
    error_declaration = "class ToolCallError(Exception):\n    toolName: str"
    declaration = (
        f"from typing import {', '.join(imports)}\n\n"
        f"{error_declaration}\n\n"
        f"{class_block}class Tools(Protocol):\n{body}\n\ntools: Tools"
    )
    return f"{SDK_INSTRUCTIONS}\n\n```python\n{declaration}\n```"


if __name__ == "__main__":
    sys.stdout.write(render_tools_sdk_py(tool_schemas.TOOLS))
