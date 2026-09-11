"""
The `ticker` command: the one way in.

    ticker                    start Ticker and open it in your browser
    ticker --headless         start it without opening anything
    ticker mcp                MCP over stdio -- what an agent launches
    ticker ask "QUESTION"     ask your data, answered by a model you run
    ticker status             what's connected and how fresh
    ticker import FILE        import an Apple Health export from the terminal
    ticker agent ...          the strap-side half of a two-machine setup
    ticker sync --once        one cloud sync without the app, for cron
    ticker rollup | backfill  maintenance

Starting Ticker when it is already running opens the running one: one
database, one Ticker.

`ticker mcp` bridges into the running app through its /mcp endpoint, so an
agent sees exactly what the page does -- on another machine too, with
--server. When nothing is running it answers from the database directly,
read-only, so an agent still works before the app has ever been started.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote

from ticker import config as tconfig

log = logging.getLogger("ticker")

# Subcommands that are an existing module's own command line, unchanged.
PASSTHROUGH = {
    "agent": "ticker.agent.main",
    "sync": "ticker.ingest.sync",
    "rollup": "ticker.db.rollup",
    "backfill": "ticker.db.backfill",
}

SUBCOMMANDS = """\
other commands:
  ticker mcp [--server URL]   MCP over stdio, for Claude Code, Codex, Claude Desktop
  ticker ask "QUESTION"       ask your data, answered by Ollama or another local model
  ticker status [--json]      what's connected and how fresh
  ticker import FILE          import an Apple Health export
  ticker agent ...            stream a strap to a Ticker on another machine
  ticker sync --once          one cloud sync without the app
  ticker rollup | backfill    maintenance
"""

# How long `ticker mcp` stops trying a Ticker that didn't answer before it
# tries again. On Windows a refused connection to localhost can take a
# couple of seconds, which is not a cost to pay on every message.
BRIDGE_RETRY_SEC = 30.0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PASSTHROUGH:
        return importlib.import_module(PASSTHROUGH[argv[0]]).main(argv[1:])
    if argv and argv[0] == "mcp":
        return mcp_main(argv[1:])
    if argv and argv[0] == "ask":
        return ask_main(argv[1:])
    if argv and argv[0] == "status":
        return status_main(argv[1:])
    if argv and argv[0] == "import":
        return import_main(argv[1:])
    return run_main(argv)


def gui_main() -> int:
    """The entry for a launch with no terminal: the packaged app, or the
    `ticker-app` launcher. Output goes to a log beside the database, since
    there is nowhere else for it to go -- and in a windowed build printing
    to a missing console would raise instead."""
    if getattr(sys, "frozen", False) or sys.stdout is None or sys.stderr is None:
        log_path = tconfig.DB_PATH.parent / "ticker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log_file
    return main()


# -- ticker (the app) ------------------------------------------------------

def page_url(host: str, port: int) -> str:
    """Where a browser on this machine reaches a server bound to host."""
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return "http://{}:{}".format(shown, port)


def already_running(url: str, timeout: float = 1.5) -> bool:
    """Is a Ticker answering at url? /health needs no token."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health",
                                    timeout=timeout) as response:
            return json.loads(response.read() or b"{}").get("app") == "Ticker"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def open_page(url: str, token: Optional[str] = None) -> None:
    # The page moves a token out of the address bar as soon as it loads.
    target = url + ("/?token=" + quote(token) if token else "/")
    try:
        webbrowser.open(target)
    except Exception:
        log.info("open %s in your browser", target)


def run_main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ticker", description="Start Ticker: sync, record, and serve "
        "your health data to you and your agents.",
        epilog=SUBCOMMANDS, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headless", action="store_true",
                        help="don't open the browser (a server, a login item)")
    parser.add_argument("--host", default=tconfig.API_HOST)
    parser.add_argument("--port", type=int, default=tconfig.API_PORT)
    parser.add_argument("--token", default=tconfig.API_TOKEN,
                        help="shared secret; required to serve beyond this machine")
    parser.add_argument("--allow-remote", action="store_true",
                        default=tconfig.API_ALLOW_REMOTE,
                        help="permit listening somewhere other than loopback")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    # Imported here so that `ticker mcp`, which an agent may launch many
    # times a day, doesn't pay for the whole app on every start.
    from ticker.api import server as apiserver
    from ticker.app.runtime import Runtime

    url = page_url(args.host, args.port)
    if already_running(url):
        log.info("Ticker is already running at %s", url)
        if not args.headless:
            open_page(url, args.token)
        return 0
    try:
        apiserver.check_bind(args.host, args.token, args.allow_remote)
    except apiserver.RemoteBindRefused as exc:
        print(exc, file=sys.stderr)
        return 2

    runtime = Runtime(args.db, host=args.host, port=args.port, token=args.token,
                      allow_remote=args.allow_remote)
    try:
        runtime.start()
    except OSError as exc:
        print("cannot listen on {}:{} - {}".format(args.host, args.port, exc),
              file=sys.stderr)
        return 1

    log.info("Ticker is running at %s  (data: %s)", runtime.url, runtime.db_path)
    log.info("connect an agent with:  claude mcp add ticker -- ticker mcp")
    if not args.headless:
        open_page(url, args.token)

    stopping = apiserver.install_stop_handlers()
    try:
        # Short timed waits, so Ctrl+C is noticed promptly on Windows too.
        while not stopping.wait(0.5):
            if runtime.stop_requested:
                break
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stopping")
        runtime.stop()
    return 0


# -- ticker mcp ------------------------------------------------------------

class _Unreachable(Exception):
    pass


class Bridge:
    """MCP messages from stdin, answered by a running Ticker's /mcp -- or,
    when none answers, by the same tools reading the database directly."""

    def __init__(self, url: str, token: Optional[str] = None,
                 required: bool = False, db_path: Optional[Path] = None,
                 timeout: float = 30.0, clock=time.monotonic,
                 profile: str = "full"):
        self.url = url
        self.token = token
        self.required = required
        self.db_path = db_path
        self.profile = profile
        self.timeout = timeout
        self._clock = clock
        self._down_until = 0.0
        self._local = None
        self._local_db = None

    def handle(self, message: Any) -> Any:
        if self._clock() >= self._down_until:
            try:
                return self._forward(message)
            except _Unreachable as exc:
                if self.required:
                    return _errors_for(message, "no Ticker answering at {}: {}".format(
                        self.url, exc))
                log.info("no Ticker at %s (%s); answering from the database",
                         self.url, exc)
                self._down_until = self._clock() + BRIDGE_RETRY_SEC
        return self._local_server().handle(message)

    def close(self) -> None:
        if self._local_db is not None:
            self._local_db.close()

    def _forward(self, message: Any) -> Any:
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status == 202:
                    return None
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("jsonrpc"):
                return parsed           # the server's own JSON-RPC error
            detail = parsed.get("error") if isinstance(parsed, dict) else exc.reason
            return _errors_for(message, "Ticker at {} answered {}: {}".format(
                self.url, exc.code, detail))
        except (urllib.error.URLError, OSError) as exc:
            raise _Unreachable(getattr(exc, "reason", exc))

    def _local_server(self):
        if self._local is None:
            from ticker.mcp.server import build
            self._local, self._local_db = build(self.db_path, profile=self.profile)
        return self._local


def _errors_for(message: Any, text: str) -> Any:
    """A JSON-RPC error reply to every request in `message`; None for
    notifications, which never get replies."""
    def one(item):
        if isinstance(item, dict) and "id" in item and "method" in item:
            return {"jsonrpc": "2.0", "id": item["id"],
                    "error": {"code": -32603, "message": text}}
        return None
    if isinstance(message, list):
        replies = [reply for reply in map(one, message) if reply is not None]
        return replies or None
    return one(message)


def mcp_main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ticker mcp",
        description="MCP over stdio for Claude Code, Codex and Claude Desktop. "
                    "Bridges into the running Ticker, or reads the database "
                    "directly when none is running.")
    parser.add_argument("--server", default=None,
                        help="a Ticker to bridge into, such as "
                             "http://homelab:8477 (default: this machine's)")
    parser.add_argument("--token", default=tconfig.API_TOKEN)
    parser.add_argument("--db", type=Path, default=None,
                        help="the database to read when no Ticker is running")
    parser.add_argument("--compact", action="store_true",
                        help="the smaller tool set, for small local models")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    from ticker.mcp.server import serve

    # stdout belongs to the protocol; anything else printed goes to stderr.
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr
    base = (args.server or page_url(tconfig.API_HOST, tconfig.API_PORT)).rstrip("/")
    profile = "compact" if args.compact else "full"
    bridge = Bridge(base + ("/mcp?profile=compact" if args.compact else "/mcp"),
                    token=args.token, required=args.server is not None,
                    db_path=args.db, profile=profile)
    if sys.stdin.isatty():
        log.warning("ticker mcp speaks MCP over stdin/stdout and is meant to be "
                    "launched by an agent: claude mcp add ticker -- ticker mcp")
    try:
        serve(bridge, sys.stdin.buffer, protocol_out)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
    return 0


# -- ticker ask -------------------------------------------------------------

def _print_safely(text: str, stream=None) -> None:
    """print() that can't fail on a console whose code page lacks a
    character the model chose -- an arrow, a degree sign, an emoji."""
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding), file=stream)


def ask_main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ticker ask",
        description="Ask a question about your data, answered by a model you "
                    "run: Ollama by default, or any OpenAI-compatible server.")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--model", default=None,
                        help="model name, e.g. qwen3:14b (default: the one "
                             "chosen on the page)")
    parser.add_argument("--url", default=None,
                        help="model server (default: the page's choice, or "
                             "Ollama at http://127.0.0.1:11434)")
    parser.add_argument("--full", action="store_true",
                        help="show the model every tool, for large models")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    from ticker.app import assistant as ask
    from ticker.app.settings import Settings
    from ticker.mcp.readonly import ReadOnlyDatabase
    from ticker.mcp.tools import Tools

    db_path = args.db or tconfig.DB_PATH
    settings = Settings.beside(db_path)
    client = ask.make_client(args.url or settings.llm_url,
                             args.model or settings.llm_model,
                             context=tconfig.LLM_CONTEXT,
                             timeout=tconfig.LLM_TIMEOUT_SEC)
    if not client.model:
        try:
            models = client.models()
        except ask.LlmError as exc:
            _print_safely(ask.friendly(exc), sys.stderr)
            return 1
        _print_safely("pick a model with --model; this server has: {}".format(
            ", ".join(models) or "none yet -- try `ollama pull qwen3:8b`"), sys.stderr)
        return 2

    readonly = ReadOnlyDatabase(db_path)
    assistant = ask.Assistant(
        Tools(readonly, profile="full" if args.full else "compact"), client)

    def show(step):
        # ASCII markers: a legacy Windows console can't print a tick.
        _print_safely("  {} {} {}".format(
            "-" if step.get("ok") else "x", step["tool"],
            json.dumps(step.get("arguments") or {})), sys.stderr)

    try:
        result = assistant.ask(" ".join(args.question), on_step=show)
    except ask.LlmError as exc:
        _print_safely(ask.friendly(exc), sys.stderr)
        return 1
    finally:
        readonly.close()
    _print_safely(result["answer"])
    return 0


# -- ticker status ----------------------------------------------------------

def _ago(text: Optional[str]) -> str:
    if not text:
        return "never"
    from ticker.model import now_utc, parse_iso
    seconds = max(0.0, (now_utc() - parse_iso(text)).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return "{:.0f} min ago".format(seconds / 60)
    if seconds < 129600:
        return "{:.0f} h ago".format(seconds / 3600)
    return "{:.0f} days ago".format(seconds / 86400)


def status_main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="ticker status",
                                     description="What Ticker has, and how fresh.")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from ticker.mcp.readonly import ReadOnlyDatabase
    from ticker.mcp.tools import ToolError, Tools

    readonly = ReadOnlyDatabase(args.db)
    try:
        overview = Tools(readonly).call("get_overview", {})
    except ToolError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        readonly.close()
    url = page_url(tconfig.API_HOST, tconfig.API_PORT)
    running = already_running(url)

    if args.json:
        print(json.dumps(dict(overview, running=running,
                              url=url if running else None), indent=2))
        return 0

    # ASCII only: a Windows console on a legacy code page can't print a tick.
    print("Ticker is running at {}".format(url) if running
          else "Ticker is not running -- start it with `ticker`")
    print("Data     {}".format(readonly.path))
    print("\nSources")
    shown = [s for s in overview["sources"]
             if s["last_data"] or s["type"] == "cloud sync"]
    if not shown:
        print("  nothing connected yet -- start `ticker` and connect from the page")
    for source in shown:
        marker = ("[x]" if source.get("sync_errors") else
                  "[-]" if not source["enabled"] else "[ok]")
        detail = "last data {}".format(_ago(source["last_data"]))
        if source.get("last_sync"):
            detail += ", synced {}".format(_ago(source["last_sync"]))
        print("  {:4} {} ({}, {}) - {}".format(marker, source["name"],
                                               source["vendor"], source["type"],
                                               detail))
        # One line per distinct failure; a missing token fails every metric.
        grouped = {}
        for metric, error in (source.get("sync_errors") or {}).items():
            grouped.setdefault(error, []).append(metric)
        for error, metrics in grouped.items():
            print("         {}{}".format(
                "" if len(grouped) == 1 else ", ".join(metrics) + ": ", error))
    if overview["metrics"]:
        print("\nMetrics")
        for metric in overview["metrics"]:
            print("  {:22} {:12} {} to {}  ({})".format(
                metric["metric"], metric["unit"], metric["first"][:10],
                metric["last"][:10], ", ".join(metric["sources"])))
    for note in overview["notes"]:
        print("\n  ! {}".format(note))
    print("\nAgents: claude mcp add ticker -- ticker mcp")
    return 0


# -- ticker import ----------------------------------------------------------

def import_main(argv: List[str]) -> int:
    """`ticker import FILE`, the format worked out from the file -- or named
    first: `ticker import apple_health export.zip`."""
    from ticker.ingest import importer
    return importer.main(argv)


if __name__ == "__main__":
    raise SystemExit(gui_main())
