# -*- coding: utf-8 -*-
"""MCP proxy: project other configured ZCode MCP servers into run_code bindings.

DSH's PTC executor binds EVERY agent-visible tool, including MCP tools. This
module lets the Agent Mode run_code server do the same for ZCode: it enumerates
the MCP servers declared in the user and workspace configuration, connects as a
minimal MCP client (stdio + Streamable HTTP), and exposes their tools as
run_code bindings.

Behavior mirrors DSH where applicable:
- Tool names are exposed bare (``tools.get_me``); collisions with built-ins or
  between servers are skipped (first server in config order wins).
- Concurrency classification is FAIL-CLOSED EXCLUSIVE for undeclared tools
  (DSH: an undeclared execution mode classifies exclusive), so every proxied
  call runs alone, in submission order.
- A server that cannot be reached is skipped with a recorded note; the rest of
  the registry stays available.

Only the Python standard library is used.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
DISCOVERY_TIMEOUT_S = 15


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


# DSH budgets are configuration; long proxied MCP calls (CI builds via remote
# servers etc.) get a generous, env-overridable default.
CALL_TIMEOUT_S = _env_int("AGENT_MODE_MCP_CALL_TIMEOUT_S", 600)
CACHE_TTL_S = 600
MAX_TOOLS_PER_SERVER = 100
MAX_TOOLS_PER_SERVER_TOTAL = 300  # paginate up to this many tools per server

BUILTIN_BINDING_NAMES = {"pwsh", "read", "write", "edit", "glob", "grep", "web_fetch"}
OWN_SERVER_NAMES = {"agent-mode", "plugin_agent-mode", "plugin_agent-mode_agent-mode"}

# Placeholder tokens we cannot resolve from an MCP server process (plugin paths
# are private to the client); such stdio servers are skipped rather than broken.
_UNRESOLVED_PLACEHOLDER = re.compile(r"\$\{[A-Z_]+\}")


class _JsonRpcError(Exception):
    pass


class ProxyNote:
    """One human-readable line about a connected or skipped server."""

    def __init__(self, server: str, message: str) -> None:
        self.server = server
        self.message = message


class _StdioConnection:
    """One MCP stdio server process: line-delimited JSON-RPC over stdin/stdout."""

    def __init__(self, server_id: str, command: list[str], env: dict | None) -> None:
        spawn_env = dict(os.environ)
        if env:
            spawn_env.update({k: os.path.expandvars(v) for k, v in env.items() if isinstance(v, str)})
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=spawn_env,
        )
        self._next_id = 0

    def request(self, method: str, params: dict | None = None, timeout_s: float = DISCOVERY_TIMEOUT_S) -> dict:
        if self._process.stdin is None or self._process.stdout is None:
            raise _JsonRpcError("stdio server closed")
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            self._process.stdin.flush()
        except OSError as error:
            raise _JsonRpcError(f"stdio write failed: {error}") from None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = self._process.stdout.readline()
            if not line:
                raise _JsonRpcError("stdio server closed the stream")
            try:
                message = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise _JsonRpcError(str(message["error"].get("message", message["error"])))
                return message.get("result") or {}
        raise _JsonRpcError(f"timeout waiting for {method} response")

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            self._process.stdin.flush()
        except OSError:
            pass

    def close(self) -> None:
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.terminate()
            self._process.wait(timeout=3)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass


class _HttpConnection:
    """Minimal MCP Streamable HTTP client (JSON responses; SSE tolerated)."""

    def __init__(self, server_id: str, url: str, headers: dict | None) -> None:
        self._url = url
        self._headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "agent-mode-ptc-proxy",
        }
        for key, value in (headers or {}).items():
            if isinstance(key, str) and isinstance(value, str):
                self._headers[key] = value
        self._session: str | None = None
        self._next_id = 0

    def _post(self, payload: dict, expect_response: bool = True) -> dict | None:
        data = json.dumps(payload).encode("utf-8")
        headers = dict(self._headers)
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        request = urllib.request.Request(self._url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_S) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session = session_id
                body = response.read()
        except urllib.error.HTTPError as error:
            raise _JsonRpcError(f"HTTP {error.code}") from None
        except (urllib.error.URLError, OSError) as error:
            raise _JsonRpcError(f"HTTP request failed: {error}") from None
        if not expect_response:
            return None
        return self._extract_response(body)

    def _extract_response(self, body: bytes) -> dict | None:
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        if text.startswith("{") or text.startswith("["):
            try:
                return self._pick(json.loads(text))
            except Exception:
                return None
        # text/event-stream: collect data: payloads across SSE events.
        for chunk in text.split("\n\n"):
            data_lines = [line[5:].strip() for line in chunk.splitlines() if line.startswith("data:")]
            candidate = "".join(data_lines)
            if not candidate:
                continue
            try:
                picked = self._pick(json.loads(candidate))
            except Exception:
                continue
            if picked is not None:
                return picked
        return None

    def _pick(self, parsed: Any) -> dict | None:
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for item in candidates:
            if isinstance(item, dict) and ("result" in item or "error" in item):
                return item
        return None

    def request(self, method: str, params: dict | None = None, timeout_s: float = DISCOVERY_TIMEOUT_S) -> dict:
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = self._post(payload)
        if response is None:
            raise _JsonRpcError(f"no response body for {method}")
        if "error" in response:
            raise _JsonRpcError(str(response["error"].get("message", response["error"])))
        return response.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, expect_response=False)

    def close(self) -> None:
        # Streamable HTTP sessions expire server-side; nothing to tear down.
        return None


def server_configs() -> list[tuple[str, dict]]:
    """Enumerate MCP servers from user and workspace ZCode configuration.

    AGENT_MODE_MCP_CONFIG (set by tests) prepends one extra configuration file.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".zcode", "cli", "config.json"),
        os.path.join(os.getcwd(), ".zcode", "config.json"),
        os.path.join(os.getcwd(), "zcode.json"),
    ]
    override = os.environ.get("AGENT_MODE_MCP_CONFIG")
    if override:
        candidates.insert(0, override)
    configs: list[tuple[str, dict]] = []
    for path in candidates:
        try:
            with io.open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        servers = ((data.get("mcp") or {}).get("servers")) or data.get("mcpServers") or {}
        if isinstance(servers, dict):
            for name, config in servers.items():
                if isinstance(name, str) and isinstance(config, dict) and name.lower() not in OWN_SERVER_NAMES:
                    configs.append((name, config))
    return configs


def _resolve_config(server_id: str) -> dict | None:
    return next((c for n, c in server_configs() if n == server_id), None)


def _server_type(config: dict) -> str:
    declared = str(config.get("type") or "")
    if declared == "http" or (declared != "stdio" and config.get("url")):
        return "http"
    return "stdio"


def _stdio_argv(config: dict) -> list[str]:
    command = config.get("command")
    args_list = config.get("args") or []
    if not isinstance(command, str) or not command:
        raise _JsonRpcError("missing command")
    argv = [os.path.expandvars(command)] + [a for a in args_list if isinstance(a, str)]
    for token in argv:
        if _UNRESOLVED_PLACEHOLDER.search(token):
            raise _JsonRpcError(f"unresolvable placeholder in command: {token}")
    return argv


def _handshake(connection: Any) -> None:
    connection.request("initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "agent-mode-ptc-proxy", "version": "0.1.0"},
    })
    connection.notify("notifications/initialized", {})


def _open_connection(server_id: str, config: dict) -> Any:
    if _server_type(config) == "http":
        return _HttpConnection(server_id, str(config["url"]), config.get("headers"))
    return _StdioConnection(server_id, _stdio_argv(config), config.get("env"))


class ProxyRegistry:
    """The discovered proxied tools plus the client connections to reach them."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}  # binding name -> {server, tool, inputSchema, description}
        self.notes: list[ProxyNote] = []
        self.discovered_at: float = 0.0

    def _list_tools_paginated(self, connection: Any) -> list[dict]:
        """tools/list with cursor pagination, up to MAX_TOOLS_PER_SERVER_TOTAL."""
        collected: list[dict] = []
        cursor: str | None = None
        for _ in range(16):  # hard bound on pages
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            result = connection.request("tools/list", params or None)
            collected.extend(item for item in (result.get("tools") or []) if isinstance(item, dict))
            if len(collected) >= MAX_TOOLS_PER_SERVER_TOTAL:
                break
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return collected[:MAX_TOOLS_PER_SERVER_TOTAL]

    def discover(self, force: bool = False) -> None:
        if not force and self.tools and (time.monotonic() - self.discovered_at) < CACHE_TTL_S:
            return
        tools: dict[str, dict] = {}
        notes: list[ProxyNote] = []
        for server_id, config in server_configs():
            try:
                connection = _open_connection(server_id, config)
                try:
                    _handshake(connection)
                    raw_tools = self._list_tools_paginated(connection)
                finally:
                    connection.close()
            except _JsonRpcError as error:
                notes.append(ProxyNote(server_id, f"skipped: {error}"))
                continue
            except Exception as error:
                notes.append(ProxyNote(server_id, f"skipped: {type(error).__name__}: {error}"))
                continue
            taken = 0
            for tool in raw_tools[:MAX_TOOLS_PER_SERVER]:
                if not isinstance(tool, dict):
                    continue
                name = str(tool.get("name") or "")
                if not name or name in BUILTIN_BINDING_NAMES or name in tools or name == "run_code":
                    continue
                tools[name] = {
                    "server": server_id,
                    "tool": name,
                    "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                    "description": str(tool.get("description") or ""),
                }
                taken += 1
            notes.append(ProxyNote(server_id, f"connected; {taken} tool(s) projected"))
        self.tools = tools
        self.notes = notes
        self.discovered_at = time.monotonic()
        write_cache(self)

    def call(self, binding_name: str, args: dict) -> Any:
        tool = self.tools.get(binding_name)
        if tool is None:
            raise KeyError(binding_name)
        server_id, tool_name = tool["server"], tool["tool"]
        config = _resolve_config(server_id)
        if config is None:
            raise RuntimeError(f"server {server_id} disappeared from configuration")
        try:
            connection = _open_connection(server_id, config)
            try:
                _handshake(connection)
                result = connection.request("tools/call", {"name": tool_name, "arguments": args}, timeout_s=CALL_TIMEOUT_S)
            finally:
                connection.close()
        except _JsonRpcError as error:
            raise RuntimeError(f"MCP {server_id}.{tool_name} failed: {error}") from None
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP {server_id}.{tool_name}: malformed result")
        if result.get("isError"):
            raise RuntimeError(_flatten_content(result.get("content")) or "tool call failed")
        value: dict = {}
        structured = result.get("structuredContent")
        if structured is not None:
            value["structured"] = structured
        text = _flatten_content(result.get("content"))
        value["text"] = text
        return value


def _flatten_content(content: Any) -> str:
    """Join an MCP tools/call content array's text blocks (the model-facing text)."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def cache_path() -> str:
    override = os.environ.get("AGENT_MODE_PROXY_CACHE")
    if override:
        return override
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".zcode", "agent-mode-mcp-tools.json")


def write_cache(registry: "ProxyRegistry") -> None:
    """Persist the discovered bindings for the UserPromptSubmit hook to surface."""
    entries = []
    for name, tool in sorted(registry.tools.items()):
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        entries.append({
            "name": name,
            "server": tool["server"],
            "description": (tool.get("description") or "")[:200],
            "required": sorted(k for k in properties if k in required),
            "optional": sorted(k for k in properties if k not in required),
        })
    payload = {
        "discoveredAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "notes": [{"server": note.server, "message": note.message} for note in registry.notes],
        "tools": entries,
    }
    try:
        with io.open(cache_path(), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass  # the cache is a best-effort surface; bindings still work


PROXY = ProxyRegistry()
