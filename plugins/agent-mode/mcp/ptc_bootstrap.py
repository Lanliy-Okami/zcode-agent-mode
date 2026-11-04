# -*- coding: utf-8 -*-
"""Agent Mode PTC bootstrap — the child side of run_code (fresh Python process per call).

Port of the DSH Python PTC runtime's bootstrap (packages/experimental/ptc-runtime-python/py/bootstrap.py),
kept to the model-visible contract:

- The program runs as the BODY of an async Python function: top-level `await` and `return` work.
- Exactly two names are bound at run time: `tools` and `ToolCallError`.
- Binding calls travel as JSON-lines over the protocol channel (DSH's fd 3): a `call` frame per
  invocation, one `reply` frame back per call, and a final `done` frame for the run.
- A failed binding call raises `ToolCallError` whose `toolName` identifies the failed tool.
- The completion value must be lossless JSON; `bytes`/non-JSON values and over-budget values are
  rejected explicitly (`invalid-output` / `output-limit`) instead of being rounded or truncated.
- stdout/stderr stay clear for the program's own output; the host captures them as `logs`.

Protocol (newline-delimited JSON on the channel):
  child -> host: {"type":"call","id":N,"name":...,"args":...}
                 {"type":"done","value":...} | {"type":"done","error":{"kind":...,"message":...}}
  host  -> child: {"type":"reply","id":N,"ok":true,"value":...}
                 | {"type":"reply","id":N,"ok":false,"error":"message"}

Program source arrives as UTF-8 JSON on trusted binary stdin: {"program": "..."}.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import threading
import traceback

PROTOCOL_FD = 3
MAX_VALUE_BYTES = 32768  # DSH maxValueBytes default: the done frame's completion value budget.


class ToolCallError(Exception):
    """Raised for a failed tool call; `toolName` identifies the failed tool."""

    def __init__(self, message: str, toolName: str) -> None:  # noqa: N803 - wire contract name
        super().__init__(message)
        self.toolName = toolName


class _Channel:
    """Newline-delimited JSON channel over one duplex socket (DSH's fd 3)."""

    def __init__(self, sock: "socket.socket") -> None:
        self._writer = sock.makefile("wb", buffering=0)
        self._reader_file = sock.makefile("rb", buffering=0)
        self._sock = sock
        self._write_lock = threading.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._pending_lock = threading.Lock()
        self._next_call_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._reader.start()

    def _send(self, frame: dict) -> None:
        data = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        with self._write_lock:
            self._writer.write(data)
            self._writer.flush()

    def _read_loop(self) -> None:
        for line in self._reader_file:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(frame, dict) or frame.get("type") != "reply":
                continue
            call_id = frame.get("id")
            with self._pending_lock:
                future = self._pending.pop(call_id, None)
            if future is None or self._loop is None:
                continue
            loop = self._loop
            if frame.get("ok") is True:
                loop.call_soon_threadsafe(future.set_result, frame.get("value"))
            else:
                message = frame.get("error")
                if not isinstance(message, str):
                    message = "tool call failed"
                loop.call_soon_threadsafe(future.set_exception, ToolCallError(message, ""))

    async def call(self, name: str, args) -> object:
        if self._loop is None:
            raise RuntimeError("channel not started")
        with self._pending_lock:
            self._next_call_id += 1
            call_id = self._next_call_id
            future: asyncio.Future = self._loop.create_future()
            self._pending[call_id] = future
        self._send({"type": "call", "id": call_id, "name": name, "args": args})
        try:
            return await future
        except ToolCallError as error:
            # The reply frame carries only the message; re-attach the binding name
            # so ToolCallError.toolName always identifies the failed tool.
            raise ToolCallError(str(error), name) from None


class _ToolsNamespace:
    """`tools` binding: attribute access for regular names, subscript for exotic ones."""

    def __init__(self, channel: _Channel) -> None:
        self._channel = channel

    def _make(self, name: str):
        async def binding(args):
            return await self._channel.call(name, args)

        binding.__name__ = name
        return binding

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._make(name)

    def __getitem__(self, name: str):
        return self._make(name)


def _open_channel(share_blob: str | None) -> "socket.socket":
    """Open the duplex protocol channel (DSH's fd 3).

    Windows: the socket arrives as a WSADuplicateSocket blob (socket.share on the
    host side, fromshare here). POSIX: the inherited fd is named in the environment.
    """
    if share_blob:
        return socket.fromshare(base64.b64decode(share_blob))
    return socket.socket(fileno=int(os.environ["PTC_CHANNEL_FD"]))


def _settle_error(kind: str, message: str) -> None:
    channel._send({"type": "done", "error": {"kind": kind, "message": message}})


def _check_value(value) -> bytes:
    """Serialize the completion value; reject non-lossless or over-budget values explicitly."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise _InvalidOutput(f"completion value is not lossless JSON: {error}") from None
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_VALUE_BYTES:
        raise _OutputLimit(
            f"completion value exceeds the {MAX_VALUE_BYTES} byte budget ({len(encoded_bytes)} bytes); "
            "extract just what you need"
        )
    return encoded_bytes


class _InvalidOutput(Exception):
    pass


class _OutputLimit(Exception):
    pass


def main() -> int:
    global channel
    raw = sys.stdin.buffer.read()
    try:
        request = json.loads(raw.decode("utf-8"))
        program = request["program"]
        if not isinstance(program, str):
            raise ValueError("program must be a string")
    except Exception as error:
        sys.stderr.write(f"[agent-mode-ptc] malformed run request: {error}\n")
        return 2

    channel = _Channel(_open_channel(request.get("channel_share")))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    channel.start(loop)

    tools = _ToolsNamespace(channel)
    namespace = {
        "__name__": "__run_code__",
        "__builtins__": __builtins__,
        "tools": tools,
        "ToolCallError": ToolCallError,
    }

    async def run() -> None:
        # Compile the program as the body of an async function (DSH py/bootstrap.py
        # step 6): AST-SPLICING the parsed body into an `async def` rewrites NO
        # text, so multi-line string literals (triple-quoted YAML etc.) survive
        # byte-exact, and every statement keeps its original line number for
        # tracebacks. Modern CPython's ast.parse accepts module-level
        # `return`/`await`, which is what makes the function-body contract work.
        import ast

        try:
            module = ast.parse(program, filename="<run_code program>")
            wrapper = ast.AsyncFunctionDef(
                name="__ptc_program__",
                args=ast.arguments(
                    posonlyargs=[], args=[], vararg=None,
                    kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[],
                ),
                body=module.body or [ast.Pass()],
                decorator_list=[],
                returns=None,
            )
            anchor = module.body[0] if module.body else ast.parse("pass").body[0]
            ast.copy_location(wrapper, anchor)
            wrapped = ast.Module(body=[wrapper], type_ignores=[])
            ast.fix_missing_locations(wrapped)
            code_object = compile(wrapped, "<run_code program>", "exec", dont_inherit=True)
        except Exception:
            message = traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()
            _settle_error("exception", message)
            return
        exec(code_object, namespace)  # noqa: S102 - model program, DSH PTC contract
        try:
            value = await namespace["__ptc_program__"]()
        except _InvalidOutput:
            raise
        except ToolCallError as error:
            message = f"{type(error).__name__}: {error}"
            _settle_error("exception", message)
            return
        except BaseException:
            message = traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()
            _settle_error("exception", message)
            return
        try:
            encoded = _check_value(value)
        except _InvalidOutput as error:
            _settle_error("invalid-output", str(error))
            return
        except _OutputLimit as error:
            _settle_error("output-limit", str(error))
            return
        done = {"type": "done"}
        if value is not None:
            done["value"] = json.loads(encoded.decode("utf-8"))
        channel._send(done)

    try:
        loop.run_until_complete(run())
    except _InvalidOutput as error:
        _settle_error("invalid-output", str(error))
    except _OutputLimit as error:
        _settle_error("output-limit", str(error))
    except BaseException:
        message = traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()
        _settle_error("exception", message)
    finally:
        try:
            channel._writer.close()
            channel._sock.close()
        except OSError:
            pass
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
