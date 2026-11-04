# -*- coding: utf-8 -*-
"""Verify the published GitHub repo contents."""
import json
import urllib.request

def get(url):
    r = urllib.request.Request(url, headers={"User-Agent": "verify", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(r, timeout=30).read()

repo = json.loads(get("https://api.github.com/repos/Lanliy-Okami/zcode-agent-mode"))
print("visibility:", repo["visibility"], "| default branch:", repo["default_branch"], "| license:", (repo.get("license") or {}).get("spdx_id"))

mk = json.loads(get("https://raw.githubusercontent.com/Lanliy-Okami/zcode-agent-mode/main/marketplace.json"))
print("marketplace name:", mk["name"], "| plugin:", mk["plugins"][0]["name"], "| source:", mk["plugins"][0]["source"])

readme = get("https://raw.githubusercontent.com/Lanliy-Okami/zcode-agent-mode/main/README.md").decode("utf-8")
print("readme link ok:", "https://github.com/Lanliy-Okami/zcode-agent-mode" in readme and "<YOUR-USER>" not in readme)

pj = json.loads(get("https://raw.githubusercontent.com/Lanliy-Okami/zcode-agent-mode/main/plugins/agent-mode/.zcode-plugin/plugin.json"))
print("plugin manifest name:", pj["name"], "| version:", pj["version"])

tree = json.loads(get("https://api.github.com/repos/Lanliy-Okami/zcode-agent-mode/git/trees/main?recursive=1"))
blobs = [item["path"] for item in tree["tree"] if item["type"] == "blob"]
print("files on main:", len(blobs))
missing = [p for p in ["plugins/agent-mode/hooks/hooks.json", "plugins/agent-mode/mcp/run_code_server.py",
                       "plugins/agent-mode/skills/ptc-mode/SKILL.md", "tests/test_hook.py"] if p not in blobs]
print("spot-check missing:", missing or "none")
