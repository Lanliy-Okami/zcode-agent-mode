# -*- coding: utf-8 -*-
"""Plugin validation: manifest JSON, resource existence, path containment, catalog agreement."""
from __future__ import annotations

import io
import json
import os
import sys

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "agent-mode")
MARKET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "marketplace.json")

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


manifest_path = os.path.join(PLUGIN, ".zcode-plugin", "plugin.json")
manifest = json.loads(io.open(manifest_path, encoding="utf-8").read())
name = manifest["name"]
check("manifest name matches directory", name == os.path.basename(os.path.normpath(PLUGIN)))
check("name matches ^[a-z0-9][a-z0-9._-]{0,127}$", bool(__import__("re").fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", name)))
check("version is semantic 0.1.0", manifest["version"] == "0.1.0")

for key, expected in (("skills", "skills"), ("commands", "commands"), ("hooks", "hooks/hooks.json"), ("mcpServers", ".mcp.json")):
    declared = manifest.get(key)
    check(f"{key} declared", declared == expected, repr(declared))
    target = os.path.join(PLUGIN, declared if key != "hooks" else declared)
    check(f"{key} resource exists", os.path.exists(target), target)

# Path containment: no declared resource escapes the plugin root.
for key in ("skills", "commands", "hooks", "mcpServers"):
    declared = manifest[key]
    resolved = os.path.abspath(os.path.join(PLUGIN, declared))
    check(f"{key} stays within plugin root", resolved.startswith(os.path.abspath(PLUGIN) + os.sep))

# Resource internals.
mcp = json.loads(io.open(os.path.join(PLUGIN, ".mcp.json"), encoding="utf-8").read())
server = mcp["mcpServers"]["agent-mode"]
check("mcp server command is python", server["command"] == "python")
check("mcp args reference plugin root token", any("${CLAUDE_PLUGIN_ROOT}" in a for a in server["args"]))
check("mcp cwd anchored to project", server["cwd"] == "${CLAUDE_PROJECT_DIR}")

hooks = json.loads(io.open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8").read())
check("hooks register PreToolUse + UserPromptSubmit", set(hooks["hooks"].keys()) == {"PreToolUse", "UserPromptSubmit"})
for event, entries in hooks["hooks"].items():
    for entry in entries:
        for hook in entry["hooks"]:
            check(f"{event} hook is process type", hook["type"] == "process")
            check(f"{event} hook references plugin root", "${CLAUDE_PLUGIN_ROOT}" in " ".join(hook["args"]))
            check(f"{event} hook script exists", os.path.exists(os.path.join(PLUGIN, "scripts", "agent_mode_hook.py")))

for skill in ("ptc-mode", "minimal-mode"):
    path = os.path.join(PLUGIN, "skills", skill, "SKILL.md")
    check(f"skill {skill} exists", os.path.exists(path))
    head = io.open(path, encoding="utf-8").read().split("---")[1]
    check(f"skill {skill} frontmatter has name+description", "name:" in head and "description:" in head)

for command in ("ptc", "minimal", "normal"):
    path = os.path.join(PLUGIN, "commands", "agent-mode", f"{command}.md")
    check(f"command /agent-mode:{command} exists", os.path.exists(path))

# Marketplace catalog agreement.
catalog = json.loads(io.open(MARKET, encoding="utf-8").read())
entry = next(p for p in catalog["plugins"] if p["name"] == name)
check("catalog source matches plugin dir", entry["source"] == f"./{name}")
check("catalog version matches manifest", entry["version"] == manifest["version"])
check("catalog description matches manifest", entry["description"] == manifest["description"])
check("catalog has zh display metadata", "zh-CN" in entry.get("displayName_i18n", {}))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("PLUGIN VALIDATION PASSED")
