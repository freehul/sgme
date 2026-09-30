from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adapter import SgmeAdapter
from .lifecycle import JsonCursorStore, SessionBridge


def _add_lifecycle_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-key", required=True)
    parser.add_argument(
        "--cursor-file",
        default=".codex-sgme-cursor.json",
        help="local cursor path; conversation content is never stored here",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-sgme")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="inject profile and pull signals")
    _add_lifecycle_args(start)
    start.add_argument("--mode", default="daily")
    start.add_argument("--max-tokens", type=int, default=800)

    turn = commands.add_parser("turn", help="append one turn from JSON on stdin")
    _add_lifecycle_args(turn)

    end = commands.add_parser("end", help="trigger asynchronous refinement")
    _add_lifecycle_args(end)

    commands.add_parser("health", help="read SGME health")
    commands.add_parser("onboarding", help="read SGME MCP onboarding")

    search = commands.add_parser("search", help="search SGME memory/Wiki")
    search.add_argument("query")
    search.add_argument("--scopes", default="memory")
    search.add_argument("--limit", type=int, default=10)
    return parser


def _print_json(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
    stream.write(rendered + "\n")


def _run_lifecycle(args: argparse.Namespace, adapter: SgmeAdapter) -> Any:
    bridge = SessionBridge(
        adapter,
        session_key=args.session_key,
        cursor_store=JsonCursorStore(Path(args.cursor_file)),
    )
    if args.command == "start":
        return bridge.start(mode=args.mode, max_tokens=args.max_tokens)
    if args.command == "end":
        return bridge.end()
    payload = json.load(sys.stdin)
    if isinstance(payload, dict):
        messages = payload.get("messages")
        started_at = payload.get("started_at")
    else:
        messages = payload
        started_at = None
    if not isinstance(messages, list):
        raise TypeError("turn input must be a JSON array or an object with messages")
    return bridge.append_turn(messages, started_at=started_at)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    adapter = SgmeAdapter.from_env()
    try:
        if args.command in {"start", "turn", "end"}:
            result = _run_lifecycle(args, adapter)
        elif args.command == "health":
            result = adapter.health()
        elif args.command == "onboarding":
            result = adapter.onboarding()
        else:
            scopes = tuple(scope for scope in args.scopes.split(",") if scope)
            result = adapter.search(args.query, scopes=scopes, limit=args.limit)
        _print_json(result)
        return 0
    except (ValueError, json.JSONDecodeError, OSError, RuntimeError) as exc:
        print(f"codex-sgme: {exc}", file=sys.stderr)
        return 2
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
