"""
MCP over JSON-RPC 2.0, with no transport in it.

`McpServer.handle` takes one decoded message (or a batch of them) and
returns the reply to send, or None when there is nothing to send -- a
notification, or a response to a request this server never made. Nothing
here reads stdin or knows what an HTTP header is: `ticker.mcp.server` wraps
it in stdio and `ticker.api.server` in HTTP at /mcp, the same split as the
read API's routes.py and server.py.

Written by hand rather than with the official SDK. The SDK needs Python
3.10 and this project supports 3.9; and a tools-only server uses four
methods of the protocol, which is less code than the dependency's
configuration would be.

Two kinds of failure, as the spec intends. A malformed message, an unknown
method or an unknown tool is a JSON-RPC error: the *client* got the
protocol wrong. A tool that ran and failed -- a bad date, an unknown
metric, a query that timed out -- is a *result* with isError set, because
the model is the one that can fix it, and a JSON-RPC error never reaches
the model.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from ticker.mcp.tools import ToolError

log = logging.getLogger("ticker.mcp")

# Newest first. A client asking for one of these gets it back; anything
# else gets the newest, and the client decides whether it can live with
# that. Nothing this server does differs between them.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SERVER_NAME = "ticker"


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def package_version() -> str:
    try:
        from importlib.metadata import version
        return version("ticker")
    except Exception:
        return "dev"


def encode(message: Any) -> bytes:
    """One message as one line of UTF-8. ensure_ascii keeps it one line:
    json.dumps escapes every newline inside a string either way, and this
    also keeps a console's code page from mangling anything on the way."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True,
                      default=str).encode("utf-8")


def parse_error(detail: str) -> Dict[str, Any]:
    return _error(None, PARSE_ERROR, "parse error: {}".format(detail))


def _error(request_id, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _text_result(text: str, is_error: bool) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpServer:
    """The protocol, bound to a set of tools.

    `tools` needs `definitions()` and `call(name, arguments)`, and `name in
    tools` to say whether a tool exists; ticker.mcp.tools.Tools is the real
    one and tests hand in smaller ones.
    """

    def __init__(self, tools, instructions: Optional[str] = None,
                 version: Optional[str] = None):
        self.tools = tools
        self.instructions = instructions
        self.version = version or package_version()
        self._methods: Dict[str, Callable[[dict], Any]] = {
            "initialize": self._initialize,
            "ping": lambda params: {},
            "tools/list": lambda params: {"tools": self.tools.definitions()},
            "tools/call": self._call_tool,
            # Not advertised, answered anyway: some clients list these
            # regardless of capabilities, and an empty list is a truer
            # answer than an error.
            "resources/list": lambda params: {"resources": []},
            "resources/templates/list": lambda params: {"resourceTemplates": []},
            "prompts/list": lambda params: {"prompts": []},
        }

    def handle(self, message: Any) -> Any:
        """The reply to one decoded message or batch, or None for none."""
        if isinstance(message, list):
            # Batches left the spec in 2025-06-18 but clients speaking
            # 2025-03-26 may still send them; answering costs nothing.
            if not message:
                return _error(None, INVALID_REQUEST, "empty batch")
            replies = [reply for reply in map(self._one, message)
                       if reply is not None]
            return replies or None
        return self._one(message)

    def _one(self, message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            request_id = message.get("id") if isinstance(message, dict) else None
            return _error(_valid_id(request_id), INVALID_REQUEST,
                          "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if method is None:
            return None             # a response; this server sends no requests
        if "id" not in message:
            return None             # a notification: initialized, cancelled...
        request_id = message["id"]
        if _valid_id(request_id) is None:
            return _error(None, INVALID_REQUEST, "id must be a string or number")
        if not isinstance(method, str):
            return _error(request_id, INVALID_REQUEST, "method must be a string")

        handler = self._methods.get(method)
        if handler is None:
            return _error(request_id, METHOD_NOT_FOUND,
                          "method not found: {}".format(method))
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        try:
            result = handler(params)
        except ProtocolError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception as exc:
            log.exception("unhandled error in %s", method)
            return _error(request_id, INTERNAL_ERROR,
                          "{}: {}".format(type(exc).__name__, exc))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # -- methods ---------------------------------------------------------

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = (requested if requested in PROTOCOL_VERSIONS
                   else PROTOCOL_VERSIONS[0])
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Ticker",
                           "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(INVALID_PARAMS, "tools/call needs a tool name")
        if name not in self.tools:
            raise ProtocolError(INVALID_PARAMS, "unknown tool: {}".format(name))
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError(INVALID_PARAMS, "arguments must be an object")
        try:
            payload = self.tools.call(name, arguments)
        except ToolError as exc:
            return _text_result(str(exc), is_error=True)
        except Exception as exc:
            # Still a result, not a protocol error: the model should see
            # that the call failed, and the session should carry on.
            log.exception("tool %s failed", name)
            return _text_result("internal error in {}: {}: {}".format(
                name, type(exc).__name__, exc), is_error=True)
        # Compact separators: this text is read by a model, and whitespace
        # in a few hundred rows of numbers is tokens with nothing in them.
        return _text_result(json.dumps(payload, separators=(",", ":"),
                                       ensure_ascii=False, default=str),
                            is_error=False)


def _valid_id(value):
    """The id if JSON-RPC allows it (a string or a number), else None."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (str, int, float)) else None
