"""
Ask your data: a small agent loop over Ticker's tools, driven by a model you
run yourself.

    Ollama, its own API           http://127.0.0.1:11434
    LM Studio, llama.cpp, vLLM    http://127.0.0.1:1234/v1   (OpenAI-compatible)

The model is shown Ticker's compact tool set. When it asks for a tool, Ticker
runs it -- read-only, in-process -- and hands the result back, until the
model answers or runs out of steps. Nothing leaves the machine unless the
model server is somewhere else.

Why a loop of Ticker's own rather than sending people to a generic client: a
small model does far better when the prompt, the tools it is shown and the
size of every result are chosen for it, and those are exactly the things a
generic client knows nothing about. It is also the only way to ask about
your data with no second app to install.

Two wire formats, both over urllib. Ollama's own /api/chat is preferred for
Ollama even though it also speaks /v1, because it is the only one that lets a
request ask for a larger context window (num_ctx) -- and Ollama's default is
too small for tool use.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ticker.mcp.tools import ToolError, zone_name

log = logging.getLogger("ticker.ask")

MAX_STEPS = 6            # tool rounds before the model is told to answer
RESULT_CHARS = 4000      # one tool result, at most, in the model's context
HISTORY_TURNS = 3        # earlier questions and answers kept for follow-ups

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# Some reasoning models put their scratch work in the answer.
_THINKING = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

SYSTEM = """\
You answer questions about the user's own health data, which Ticker keeps on \
their machine. Look things up with the tools; never guess or invent numbers. \
If you don't know which metrics or dates exist, call get_overview first. \
Today is {today}; times are local ({zone}).

Keep answers short: lead with the answer, then the few numbers behind it, \
with units and dates. If the data is thin, missing, or covers only a day or \
two, say so. This is wellness data from consumer devices: don't diagnose or \
give medical advice."""


class LlmError(Exception):
    """The model server couldn't be used, and what to do about it."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Optional[dict]        # None: the model sent unreadable JSON


@dataclass
class Turn:
    content: str                     # the answer text, reasoning stripped
    tool_calls: List[ToolCall]
    message: dict                    # the reply, as the server wants it back


def detect_api(url: str) -> str:
    """'openai' for a URL ending in /v1, otherwise Ollama's own API."""
    return "openai" if urlparse(url).path.rstrip("/").endswith("/v1") else "ollama"


def is_local(url: str) -> bool:
    return (urlparse(url).hostname or "") in LOOPBACK


def _request(method: str, url: str, payload: Optional[dict] = None,
             timeout: float = 30.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise LlmError("{} answered {}: {}".format(
            url, exc.code, _error_text(exc.read()) or exc.reason))
    except (urllib.error.URLError, OSError) as exc:
        raise LlmError("can't reach {}: {}".format(url, getattr(exc, "reason", exc)))
    except ValueError as exc:
        raise LlmError("{} didn't answer with JSON: {}".format(url, exc))


def _error_text(body: bytes) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")[:300]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or parsed)[:300]


def _arguments(raw: Any) -> Optional[dict]:
    """Tool arguments as a dict: Ollama sends an object, the OpenAI format a
    JSON string. None when neither can be read."""
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _functions(definitions: Iterable[dict]) -> List[dict]:
    """MCP tool definitions in the function-calling shape both APIs take."""
    return [{"type": "function",
             "function": {"name": d["name"], "description": d["description"],
                          "parameters": d["inputSchema"]}}
            for d in definitions]


class OllamaChat:
    """Ollama's own /api/chat and /api/tags."""

    api = "ollama"

    def __init__(self, url: str, model: str, context: int = 8192,
                 timeout: float = 300.0, request: Callable = _request):
        self.url = url.rstrip("/")
        self.model = model
        self.context = context
        self.timeout = timeout
        self._request = request

    def models(self) -> List[str]:
        reply = self._request("GET", self.url + "/api/tags", timeout=5.0)
        return sorted(m["name"] for m in reply.get("models") or [] if m.get("name"))

    def complete(self, messages: List[dict], definitions: Sequence[dict]) -> Turn:
        payload: Dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": False,
            "options": {"num_ctx": self.context, "temperature": 0.2}}
        if definitions:
            payload["tools"] = _functions(definitions)
        reply = self._request("POST", self.url + "/api/chat", payload,
                              timeout=self.timeout)
        message = reply.get("message") or {}
        raw_calls = message.get("tool_calls") or []
        calls = []
        for index, raw in enumerate(raw_calls):
            function = raw.get("function") or {}
            calls.append(ToolCall(id=str(raw.get("id") or index),
                                  name=str(function.get("name") or ""),
                                  arguments=_arguments(function.get("arguments"))))
        content = message.get("content") or ""
        back = {"role": "assistant", "content": content}
        if raw_calls:
            back["tool_calls"] = raw_calls
        return Turn(_THINKING.sub("", content).strip(), calls, back)

    @staticmethod
    def tool_message(call: ToolCall, text: str) -> dict:
        return {"role": "tool", "content": text, "tool_name": call.name}


class OpenAIChat:
    """/v1/chat/completions and /v1/models, as LM Studio, llama.cpp's
    server, vLLM and Ollama's own /v1 all speak them."""

    api = "openai"

    def __init__(self, url: str, model: str, timeout: float = 300.0,
                 request: Callable = _request):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._request = request

    def models(self) -> List[str]:
        reply = self._request("GET", self.url + "/models", timeout=5.0)
        return sorted(m["id"] for m in reply.get("data") or [] if m.get("id"))

    def complete(self, messages: List[dict], definitions: Sequence[dict]) -> Turn:
        payload: Dict[str, Any] = {"model": self.model, "messages": messages,
                                   "stream": False, "temperature": 0.2}
        if definitions:
            payload["tools"] = _functions(definitions)
            payload["tool_choice"] = "auto"
        reply = self._request("POST", self.url + "/chat/completions", payload,
                              timeout=self.timeout)
        choices = reply.get("choices") or []
        if not choices:
            raise LlmError("{} answered with no choices".format(self.url))
        message = choices[0].get("message") or {}
        calls = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            calls.append(ToolCall(id=str(raw.get("id") or "call_{}".format(index)),
                                  name=str(function.get("name") or ""),
                                  arguments=_arguments(function.get("arguments"))))
        content = message.get("content") or ""
        back: Dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            # Re-encoded rather than echoed: the arguments go back as the JSON
            # string the format requires, whatever the server first sent.
            back["tool_calls"] = [
                {"id": call.id, "type": "function",
                 "function": {"name": call.name,
                              "arguments": json.dumps(call.arguments or {})}}
                for call in calls]
        return Turn(_THINKING.sub("", content).strip(), calls, back)

    @staticmethod
    def tool_message(call: ToolCall, text: str) -> dict:
        return {"role": "tool", "tool_call_id": call.id, "content": text}


def make_client(url: str, model: str, context: int = 8192,
                timeout: float = 300.0):
    if detect_api(url) == "openai":
        return OpenAIChat(url, model, timeout=timeout)
    return OllamaChat(url, model, context=context, timeout=timeout)


def friendly(error: LlmError) -> str:
    """What an LlmError means for someone looking at the Ask box."""
    text = str(error)
    if "does not support tools" in text:
        return (text + " -- pick a model that can call tools, such as qwen3, "
                "llama3.1 or mistral")
    if "can't reach" in text:
        return text + " -- is Ollama (or your model server) running?"
    if " 404" in text and "model" in text.lower():
        return text + " -- pull it first, e.g. `ollama pull qwen3:8b`"
    return text


class Assistant:
    """One question at a time, answered with the tools."""

    def __init__(self, tools, client, max_steps: int = MAX_STEPS,
                 result_chars: int = RESULT_CHARS):
        self.tools = tools
        self.client = client
        self.max_steps = max_steps
        self.result_chars = result_chars

    def system_prompt(self) -> str:
        now = self.tools.now().astimezone(self.tools.zone)
        return SYSTEM.format(today=now.strftime("%A %Y-%m-%d"),
                             zone=zone_name(self.tools.zone))

    def ask(self, question: str, history: Sequence[Tuple[str, str]] = (),
            on_step: Optional[Callable[[dict], None]] = None) -> Dict[str, Any]:
        """{'answer', 'steps'} for one question. Raises LlmError when the
        model server fails; a tool that fails is the model's to deal with."""
        messages: List[dict] = [{"role": "system", "content": self.system_prompt()}]
        for asked, answered in list(history)[-HISTORY_TURNS:]:
            messages.append({"role": "user", "content": asked})
            messages.append({"role": "assistant", "content": answered})
        messages.append({"role": "user", "content": question})
        definitions = self.tools.definitions()
        steps: List[dict] = []

        for _ in range(self.max_steps):
            turn = self.client.complete(messages, definitions)
            if not turn.tool_calls:
                return {"answer": turn.content or "(the model gave no answer)",
                        "steps": steps}
            messages.append(turn.message)
            for call in turn.tool_calls:
                step, text = self._run(call)
                messages.append(self.client.tool_message(call, text))
                steps.append(step)
                if on_step is not None:
                    on_step(step)

        # Out of steps: ask once more, with no tools on offer, so the user
        # gets what the model has rather than nothing.
        messages.append({"role": "user", "content": "That's enough looking up. "
                                                    "Answer from what you have."})
        turn = self.client.complete(messages, [])
        return {"answer": turn.content or "I couldn't finish looking that up.",
                "steps": steps, "out_of_steps": True}

    def _run(self, call: ToolCall) -> Tuple[dict, str]:
        step: Dict[str, Any] = {"tool": call.name, "arguments": call.arguments or {}}
        if call.arguments is None:
            step.update(ok=False, error="arguments weren't valid JSON")
            return step, "Error: your arguments weren't valid JSON; send an object."
        try:
            payload = self.tools.call(call.name, call.arguments)
        except ToolError as exc:
            step.update(ok=False, error=str(exc))
            return step, "Error: {}".format(exc)
        except Exception as exc:
            log.exception("tool %s failed", call.name)
            step.update(ok=False, error="internal error")
            return step, "Error: the tool failed ({}).".format(type(exc).__name__)
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False,
                          default=str)
        step["ok"] = True
        if len(text) > self.result_chars:
            text = (text[:self.result_chars]
                    + " ...[cut short: ask for a shorter range or fewer metrics]")
            step["truncated"] = True
        return step, text
