# -*- coding: utf-8 -*-
"""Rewrite JSON files without BOM (PowerShell Set-Content added one)."""
import io
import json

for p in ["plugins/agent-mode/.zcode-plugin/plugin.json", "marketplace.json"]:
    data = json.load(io.open(p, encoding="utf-8-sig"))
    with io.open(p, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(p, "version =", json.load(io.open(p, encoding="utf-8"))["version"])
