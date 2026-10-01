"""命令行入口：hooks 分发 + 调试工具。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import hooks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-sgme")
    commands = parser.add_subparsers(dest="command", required=True)

    hook = commands.add_parser("hook", help="Claude Code hook 入口（stdin 读 JSON）")
    hook.add_argument("event", choices=sorted(hooks.EVENTS))

    commands.add_parser("health", help="读取 SGME 健康状态")
    commands.add_parser("onboarding", help="读取 SGME MCP onboarding")

    refine = commands.add_parser("refine", help="触发异步提炼（默认扫全部 status=new）")
    refine.add_argument("--file-id", default=None, help="只提炼指定 L0 文件")

    search = commands.add_parser("search", help="检索 SGME 记忆")
    search.add_argument("query")
    search.add_argument("--scopes", default="memory")
    search.add_argument("--limit", type=int, default=10)
    return parser


def _print_json(value: Any) -> None:
    stream = sys.stdout
    rc = getattr(stream, "reconfigure", None)
    if callable(rc):
        rc(encoding="utf-8")
    stream.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "hook":
        return hooks.run(args.event)

    from .adapter import SgmeAdapter

    adapter = SgmeAdapter.from_env()
    try:
        if args.command == "health":
            result = adapter.health()
        elif args.command == "onboarding":
            result = adapter.onboarding()
        elif args.command == "refine":
            if args.file_id:
                result = adapter.mcp_tool("refine_trigger", file_id=args.file_id, async_mode=True)
            else:
                result = adapter.refine_trigger(async_mode=True)
        else:
            scopes = tuple(scope for scope in args.scopes.split(",") if scope)
            result = adapter.search(args.query, scopes=scopes, limit=args.limit)
        _print_json(result)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"claude-sgme: {exc}", file=sys.stderr)
        return 2
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
