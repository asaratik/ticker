"""
Tests for the Ask box's agent loop and its two wire formats.

The loop is driven by a scripted client over real tools and a real
temporary database, so a tool call in these tests reads actual rows. The
wire formats are tested over real HTTP against tests/llmstub.py, which
records what it was sent: what Ollama and an OpenAI-compatible server
receive is the contract, and it is checked on the wire.
"""

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from llmstub import StubModelServer, ollama, openai
from ticker.app import assistant
from ticker.db import queries, store
from ticker.mcp.readonly import ReadOnlyDatabase
from ticker.mcp.tools import Tools
from ticker.model import iso_utc

UTC = timezone.utc
NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


class Scripted:
    """A client that replays turns and remembers what it was shown."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.seen = []

    def complete(self, messages, definitions):
        self.seen.append((json.loads(json.dumps(messages)),
                          [d["name"] for d in definitions]))
        return self.turns.pop(0)

    @staticmethod
    def tool_message(call, text):
        return {"role": "tool", "content": text, "name": call.name}


def turn(content="", *calls):
    tool_calls = [assistant.ToolCall(str(i), name, args)
                  for i, (name, args) in enumerate(calls)]
    return assistant.Turn(content, tool_calls, {"role": "assistant", "content": content})


@pytest.fixture
def tools(tmp_path):
    path = tmp_path / "ask.sqlite3"
    conn = store.connect(path)
    ring = store.ensure_source(conn, "pull", "oura", "Ring")
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, ts, value, external_id, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        (ring, queries.metric_id(conn, "heart_rate_bpm"),
         iso_utc(NOW - timedelta(hours=1)), 58.0, "", iso_utc(NOW)))
    conn.close()
    readonly = ReadOnlyDatabase(path)
    yield Tools(readonly, zone=UTC, now=lambda: NOW, profile="compact")
    readonly.close()


# -- the loop ------------------------------------------------------------------

def test_an_answer_needing_no_tools_comes_straight_back(tools):
    client = Scripted(turn("Hello."))
    result = assistant.Assistant(tools, client).ask("hi")
    assert result == {"answer": "Hello.", "steps": []}
    system = client.seen[0][0][0]["content"]
    assert "Sunday 2026-05-10" in system and "UTC" in system
    # The compact set: no SQL for a small model.
    assert "query_sql" not in client.seen[0][1] and "get_overview" in client.seen[0][1]


def test_a_tool_call_runs_and_its_result_goes_back_to_the_model(tools):
    client = Scripted(turn("", ("get_overview", {})), turn("One source: Ring."))
    seen = []
    result = assistant.Assistant(tools, client).ask("what do I have?", on_step=seen.append)
    assert result["answer"] == "One source: Ring."
    assert seen == result["steps"] == [{"tool": "get_overview", "arguments": {}, "ok": True}]
    tool_message = client.seen[1][0][-1]
    assert tool_message["role"] == "tool" and "heart_rate_bpm" in tool_message["content"]


def test_a_failing_tool_is_the_models_to_recover_from(tools):
    client = Scripted(turn("", ("get_timeseries", {"metric": "heartrate"})),
                      turn("", ("no_such_tool", {})),
                      turn("", ("get_sleep", None)),
                      turn("Sorry, I couldn't find that."))
    result = assistant.Assistant(tools, client).ask("heart rate?")
    assert [s["ok"] for s in result["steps"]] == [False, False, False]
    assert "heart_rate_bpm" in client.seen[1][0][-1]["content"]     # the known names
    assert "unknown tool" in client.seen[2][0][-1]["content"]
    assert "valid JSON" in client.seen[3][0][-1]["content"]


def test_a_big_result_is_cut_short_and_says_so(tools):
    client = Scripted(turn("", ("get_overview", {})), turn("ok"))
    result = assistant.Assistant(tools, client, result_chars=50).ask("q")
    assert result["steps"][0]["truncated"] is True
    assert client.seen[1][0][-1]["content"].endswith("fewer metrics]")


def test_out_of_steps_it_is_asked_to_answer_with_no_tools_on_offer(tools):
    client = Scripted(turn("", ("get_overview", {})), turn("", ("get_overview", {})),
                      turn("Best I can say: one source."))
    result = assistant.Assistant(tools, client, max_steps=2).ask("q")
    assert result["out_of_steps"] is True
    assert result["answer"] == "Best I can say: one source."
    assert client.seen[-1][1] == []


def test_earlier_questions_ride_along_for_follow_ups(tools):
    client = Scripted(turn("Yes."))
    history = [("q{}".format(i), "a{}".format(i)) for i in range(5)]
    assistant.Assistant(tools, client).ask("and now?", history)
    contents = [m["content"] for m in client.seen[0][0][1:]]
    assert contents == ["q2", "a2", "q3", "a3", "q4", "a4", "and now?"]


# -- the wire formats ----------------------------------------------------------------

@pytest.fixture
def stub():
    servers = []

    def start(*replies, models=("tiny",)):
        server = StubModelServer(replies, models).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def test_ollama_is_asked_for_a_bigger_context_and_sent_tools(tools, stub):
    server = stub(ollama("", [("get_overview", {})]),
                  ollama("<think>hmm</think>You have heart rate from Ring."))
    client = assistant.OllamaChat(server.url, "tiny", context=16384)
    result = assistant.Assistant(tools, client).ask("what do I have?")
    assert result["answer"] == "You have heart rate from Ring."    # reasoning stripped

    path, body = server.requests[0]
    assert path == "/api/chat" and body["stream"] is False
    assert body["options"]["num_ctx"] == 16384
    assert body["tools"][0]["type"] == "function"
    assert "parameters" in body["tools"][0]["function"]
    tool_reply = server.requests[1][1]["messages"][-1]
    assert tool_reply["role"] == "tool" and tool_reply["tool_name"] == "get_overview"
    assert client.models() == ["tiny"]


def test_openai_compatible_servers_get_their_own_shape(tools, stub):
    server = stub(openai(None, [("get_sleep", {"start": "-7d"})]), openai("Slept fine."))
    client = assistant.make_client(server.url + "/v1", "tiny")
    assert isinstance(client, assistant.OpenAIChat)
    result = assistant.Assistant(tools, client).ask("sleep?")
    assert result["answer"] == "Slept fine."
    assert result["steps"][0]["arguments"] == {"start": "-7d"}   # decoded from a string

    path, body = server.requests[0]
    assert path == "/v1/chat/completions" and body["tool_choice"] == "auto"
    replay = server.requests[1][1]["messages"]
    call = replay[-2]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"start": "-7d"}
    assert replay[-1]["tool_call_id"] == call["id"]
    assert client.models() == ["tiny"]


def test_a_model_without_tools_is_told_to_the_user_plainly(tools, stub):
    server = stub((400, {"error": "registry.ollama.ai/library/llama2 does not support tools"}))
    with pytest.raises(assistant.LlmError) as raised:
        assistant.Assistant(tools, assistant.OllamaChat(server.url, "llama2")).ask("q")
    assert "qwen3" in assistant.friendly(raised.value)


def test_an_unreachable_server_says_so():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(assistant.LlmError) as raised:
        assistant.OllamaChat("http://127.0.0.1:{}".format(port), "tiny").models()
    assert "is Ollama" in assistant.friendly(raised.value)


def test_urls_decide_the_format_and_whether_data_stays_local():
    assert assistant.detect_api("http://127.0.0.1:11434") == "ollama"
    assert assistant.detect_api("http://127.0.0.1:1234/v1/") == "openai"
    assert assistant.is_local("http://localhost:11434")
    assert not assistant.is_local("http://gpu-box.lan:11434")
