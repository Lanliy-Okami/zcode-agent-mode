# -*- coding: utf-8 -*-
"""Bump manifest + catalog version (no BOM, keeps zh-CN intact)."""
import io
import json
import sys

NEW = sys.argv[1] if len(sys.argv) > 1 else "0.3.1"

manifest_path = "plugins/agent-mode/.zcode-plugin/plugin.json"
manifest = json.load(io.open(manifest_path, encoding="utf-8-sig"))
manifest["version"] = NEW
with io.open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2)
    handle.write("\n")

catalog_path = "marketplace.json"
catalog = json.load(io.open(catalog_path, encoding="utf-8-sig"))
catalog["plugins"][0]["version"] = NEW
with io.open(catalog_path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(catalog, handle, ensure_ascii=False, indent=2)
    handle.write("\n")

print("bumped to", NEW, ":", manifest["version"], catalog["plugins"][0]["version"])
