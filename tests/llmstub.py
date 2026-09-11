"""
A pretend model server on a free port: Ollama's /api/* and the
OpenAI-compatible /v1/*, answering POSTs from a script and recording what
it was sent -- so the Ask box's wire formats are tested over real HTTP
without a model anywhere near the test suite.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        stub = self.server.stub
        if self.path == "/api/tags":
            self._reply(200, {"models": [{"name": m} for m in stub.models]})
        elif self.path == "/v1/models":
            self._reply(200, {"data": [{"id": m} for m in stub.models]})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        stub = self.server.stub
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        stub.requests.append((self.path, body))
        reply = stub.replies.pop(0) if stub.replies else {"error": "script ran out"}
        if isinstance(reply, tuple):
            self._reply(*reply)
        else:
            self._reply(200, reply)


class StubModelServer:
    def __init__(self, replies=(), models=("tiny",)):
        self.replies = list(replies)
        self.models = list(models)
        self.requests = []
        self._httpd = None

    def start(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.stub = self
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    @property
    def url(self):
        return "http://127.0.0.1:{}".format(self._httpd.server_address[1])

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


def ollama(content="", calls=()):
    """An Ollama /api/chat reply: calls are (name, arguments) pairs."""
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"function": {"name": n, "arguments": a}}
                                 for n, a in calls]
    return {"model": "tiny", "message": message, "done": True}


def openai(content=None, calls=()):
    """An OpenAI-compatible /v1/chat/completions reply."""
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": "call_{}".format(i), "type": "function",
             "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)]
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
