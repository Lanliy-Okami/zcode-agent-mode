# -*- coding: utf-8 -*-
"""Verify the pushed v0.1.1 via the GitHub contents API (raw CDN may lag)."""
import base64
import json
import urllib.request


def api(path):
    r = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"User-Agent": "v", "Accept": "application/vnd.github+json"},
    )
    return json.loads(urllib.request.urlopen(r, timeout=30).read())


tree = api("/repos/Lanliy-Okami/zcode-agent-mode/git/trees/main?recursive=1")
blobs = {i["path"] for i in tree["tree"] if i["type"] == "blob"}
print("files on main:", len(blobs))


def content(path):
    c = api("/repos/Lanliy-Okami/zcode-agent-mode/contents/" + path + "?ref=main")
    return base64.b64decode(c["content"]).decode("utf-8")


hook = content("plugins/agent-mode/scripts/agent_mode_hook.py")
print("hook has plugin-prefixed name + matcher:", "mcp__plugin_agent-mode_agent-mode__run_code" in hook and "is_run_code" in hook)
pj = json.loads(content("plugins/agent-mode/.zcode-plugin/plugin.json"))
print("plugin version:", pj["version"])
mk = json.loads(content("marketplace.json"))
print("catalog version:", mk["plugins"][0]["version"])
readme = content("README.md")
print("readme link + tool name ok:", "https://github.com/Lanliy-Okami/zcode-agent-mode" in readme and "<YOUR-USER>" not in readme and "mcp__plugin_agent-mode_agent-mode__run_code" in readme)
cmd = content("plugins/agent-mode/commands/agent-mode/ptc.md")
print("ptc command loads skill before state write:", cmd.index("Skill tool") < cmd.index("agent-mode.json"))
