# -*- coding: utf-8 -*-
"""A minimal fake stdio MCP server for proxy tests: one tool `echo`."""
import json
import sys

for line in sys.stdin.buffer:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line.decode("utf-8"))
    except Exception:
        continue
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo the message back as text.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"message": {"type": "string", "description": "Text to echo."}},
                        "required": ["message"],
                    },
                }
            ]
        }
    elif method == "tools/call":
        args = (message.get("params") or {}).get("arguments") or {}
        text = f"echo: {args.get('message', '')}"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        result = {}
    sys.stdout.buffer.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
