"""
Ticker's MCP server over stdio, reading the database directly.

    python -m ticker.mcp.server --db D:/elsewhere/hrm_data.sqlite3

Agents launch `ticker mcp`, not this: it bridges into the running app's
/mcp, and falls back to exactly this -- the same tools, read-only against
the file -- when nothing is running. It stays runnable on its own for that
fallback's sake and for testing the stdio wire.

It speaks newline-delimited JSON-RPC over stdin and stdout, opens the
database read-only and never writes, so running it beside the app is safe:
under WAL it is one more reader, and it sees the app's writes as they
commit.

stdout belongs to the protocol. One stray print anywhere in the process
would corrupt the stream, so main() takes stdout's buffer for itself and
points sys.stdout at stderr, where anything accidental becomes a log line.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import BinaryIO, Iterable, Optional, Tuple

from ticker import config as tconfig
from ticker.mcp import protocol
from ticker.mcp.protocol import McpServer
from ticker.mcp.readonly import ReadOnlyDatabase
from ticker.mcp.tools import COMPACT_INSTRUCTIONS, INSTRUCTIONS, Tools

log = logging.getLogger("ticker.mcp")


def build(db_path: Optional[Path] = None, zone=None, profile: str = "full"
          ) -> Tuple[McpServer, ReadOnlyDatabase]:
    """The protocol over the tools over a read-only database. Shared by this
    transport, `ticker mcp`'s fallback and the app's /mcp, so they can't
    drift apart. profile="compact" is the smaller tool set for local models."""
    db = ReadOnlyDatabase(db_path)
    instructions = COMPACT_INSTRUCTIONS if profile == "compact" else INSTRUCTIONS
    return McpServer(Tools(db, zone=zone, profile=profile),
                     instructions=instructions), db


def serve(server: McpServer, stdin: Iterable[bytes], stdout: BinaryIO) -> None:
    """Answer each line of stdin until it closes.

    One message at a time, in order. Every tool is a local SQLite read with
    a time budget, so there is nothing to gain from concurrency and a good
    deal of stdout interleaving to lose.
    """
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            reply = protocol.parse_error(str(exc))
        else:
            reply = server.handle(message)
        if reply is not None:
            stdout.write(protocol.encode(reply) + b"\n")
            stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="MCP server (stdio) over the Ticker database, for agents "
                    "such as Claude Code, Codex and Claude Desktop.")
    parser.add_argument("--db", type=Path, default=None,
                        help="database file (default: {})".format(tconfig.DB_PATH))
    parser.add_argument("--compact", action="store_true",
                        help="the smaller tool set, for local models")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr

    server, db = build(args.db, profile="compact" if args.compact else "full")
    if sys.stdin.isatty():
        log.warning("this speaks MCP over stdin/stdout for an agent; agents "
                    "should launch `ticker mcp`: "
                    "claude mcp add ticker -- ticker mcp")
    log.info("serving %s (read-only)", db.path)
    try:
        serve(server, sys.stdin.buffer, protocol_out)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
