# -*- coding: utf-8 -*-
"""One-off: discover the user's real configured MCP servers (tools/list only, read-only)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "agent-mode", "mcp"))

import mcp_proxy

registry = mcp_proxy.ProxyRegistry()
registry.discover(force=True)
for note in registry.notes:
    print(f"  [{note.server}] {note.message}")
print(f"total projected bindings: {len(registry.tools)}")
for name in sorted(registry.tools):
    print("   -", name)
print("cache written to:", mcp_proxy.cache_path())
