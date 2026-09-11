"""
Tests for the MCP protocol layer.

No database and no transport: McpServer is handed a stand-in tool set and
messages go in as dicts. What's tested is the JSON-RPC contract a client
depends on -- version negotiation, which failures are protocol errors and
which are tool results, and that notifications get no reply.
"""

import json
import re

import pytest

from ticker.mcp import protocol
from ticker.mcp.protocol import McpServer
from ticker.mcp.tools import SPECS, ToolError, Tools


class FakeTools:
    def __init__(self):
        self.calls = []

    def __contains__(self, name):
        return name in ("echo", "fails", "explodes")

    def definitions(self):
        return [{"name": "echo", "inputSchema": {"type": "object"}}]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "fails":
            raise ToolError("start is after end")
        if name == "explodes":
            raise RuntimeError("database is on fire")
        return {"echo": arguments}


@pytest.fixture
def tools():
    return FakeTools()


@pytest.fixture
def server(tools):
    return McpServer(tools, instructions="read me", version="1.2.3")


def request(method, params=None, id=1):
    message = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return message


NOTIFY = {"jsonrpc": "2.0", "method": "notifications/initialized"}


# -- initialize -------------------------------------------------------------

def test_initialize_echoes_a_version_it_supports(server):
    reply = server.handle(request("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}))
    result = reply["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "ticker"
    assert result["serverInfo"]["version"] == "1.2.3"
    assert result["instructions"] == "read me"


def test_initialize_offers_its_newest_version_for_one_it_does_not_know(server):
    # The client decides whether it can live with ours; refusing outright
    # would strand a client one spec revision ahead.
    reply = server.handle(request("initialize", {"protocolVersion": "2099-01-01"}))
    assert reply["result"]["protocolVersion"] == protocol.PROTOCOL_VERSIONS[0]


def test_ping_answers_empty(server):
    assert server.handle(request("ping"))["result"] == {}


# -- notifications and responses --------------------------------------------

def test_notifications_get_no_reply(server):
    assert server.handle(NOTIFY) is None
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": 1}}) is None


def test_a_response_from_the_client_is_ignored(server):
    # This server never sends requests, so a response can only be stray.
    assert server.handle({"jsonrpc": "2.0", "id": 7, "result": {}}) is None


# -- protocol errors ----------------------------------------------------------

def test_an_unknown_method_is_method_not_found(server):
    reply = server.handle(request("sampling/createMessage"))
    assert reply["id"] == 1
    assert reply["error"]["code"] == protocol.METHOD_NOT_FOUND


def test_a_message_that_is_not_json_rpc_is_invalid(server):
    assert server.handle({"id": 1, "method": "ping"})["error"]["code"] == \
        protocol.INVALID_REQUEST
    assert server.handle("ping")["error"]["code"] == protocol.INVALID_REQUEST


def test_an_object_id_is_invalid_and_answered_with_a_null_id(server):
    reply = server.handle({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"})
    assert reply["id"] is None
    assert reply["error"]["code"] == protocol.INVALID_REQUEST


def test_params_must_be_an_object(server):
    reply = server.handle(request("tools/list", [1]))
    assert reply["error"]["code"] == protocol.INVALID_PARAMS


def test_a_parse_error_has_a_null_id():
    reply = protocol.parse_error("Expecting value")
    assert reply["id"] is None
    assert reply["error"]["code"] == protocol.PARSE_ERROR


def test_unadvertised_listings_are_empty_rather_than_errors(server):
    assert server.handle(request("resources/list"))["result"] == {"resources": []}
    assert server.handle(request("prompts/list"))["result"] == {"prompts": []}


# -- tools ------------------------------------------------------------------

def test_tools_list_comes_from_the_tool_set(server):
    tools = server.handle(request("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo"]


def test_a_tool_result_is_json_text(server):
    reply = server.handle(request("tools/call",
                                  {"name": "echo", "arguments": {"x": 1}}))
    result = reply["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"echo": {"x": 1}}


def test_missing_arguments_are_an_empty_object(server, tools):
    server.handle(request("tools/call", {"name": "echo"}))
    assert tools.calls == [("echo", {})]


def test_a_tool_error_reaches_the_model_as_a_result(server):
    # A JSON-RPC error never reaches the model; a result with isError does,
    # and the model is the one that can fix a bad date.
    result = server.handle(request("tools/call", {"name": "fails"}))["result"]
    assert result["isError"] is True
    assert "start is after end" in result["content"][0]["text"]


def test_an_unexpected_failure_is_still_a_result_not_a_crash(server):
    result = server.handle(request("tools/call", {"name": "explodes"}))["result"]
    assert result["isError"] is True
    assert "on fire" in result["content"][0]["text"]


def test_an_unknown_tool_is_a_protocol_error(server, tools):
    reply = server.handle(request("tools/call", {"name": "delete_everything"}))
    assert reply["error"]["code"] == protocol.INVALID_PARAMS
    assert tools.calls == []


def test_arguments_must_be_an_object(server):
    reply = server.handle(request("tools/call",
                                  {"name": "echo", "arguments": "x"}))
    assert reply["error"]["code"] == protocol.INVALID_PARAMS


# -- batches ----------------------------------------------------------------

def test_a_batch_gets_replies_for_its_requests_only(server):
    replies = server.handle([request("ping", id=1), NOTIFY, request("ping", id=2)])
    assert [r["id"] for r in replies] == [1, 2]


def test_an_empty_batch_is_invalid(server):
    assert server.handle([])["error"]["code"] == protocol.INVALID_REQUEST


def test_a_batch_of_notifications_gets_no_reply(server):
    assert server.handle([NOTIFY, NOTIFY]) is None


# -- encoding ---------------------------------------------------------------

def test_encode_is_one_line_even_with_newlines_inside_strings():
    # stdio frames messages by newline; one inside a message would split it.
    line = protocol.encode({"text": "a\nb", "name": "café"})
    assert b"\n" not in line
    assert json.loads(line) == {"text": "a\nb", "name": "café"}


# -- the real tool definitions ----------------------------------------------

def test_every_tool_is_read_only_with_a_closed_object_schema():
    for definition in Tools(db=None).definitions():
        schema = definition["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert definition["annotations"]["readOnlyHint"] is True
        assert definition["annotations"]["destructiveHint"] is False
        assert definition["description"]


def test_tool_names_are_unique_and_valid():
    names = [spec.name for spec in SPECS]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z_]{1,64}", name) for name in names)


def test_every_tool_has_an_implementation():
    for spec in SPECS:
        assert callable(getattr(Tools, "_" + spec.name, None)), spec.name
