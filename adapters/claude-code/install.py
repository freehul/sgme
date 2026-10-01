"""安装 Claude Code × SGME 适配器。

- 注册 MCP server 到 ~/.claude.json（mcpServers.sgme，stdio 动态透传）
- 注册 hooks 到 ~/.claude/settings.json（SessionStart / Stop / SessionEnd）
- 不改动其它 MCP server / hooks 条目；写入前自动备份；可重复运行（幂等）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from claude_sgme.config import Settings

PROJECT_DIR = Path(__file__).resolve().parent
CLAUDE_JSON = Path.home() / ".claude.json"
SETTINGS_JSON = Path.home() / ".claude" / "settings.json"

HOOK_EVENTS = [
    ("SessionStart", "session-start"),
    ("Stop", "stop"),
    ("SessionEnd", "session-end"),
]
HOOK_MARKER = "claude_sgme.cli hook"


def _venv_python() -> str:
    candidate = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    return str(candidate if candidate.exists() else Path(sys.executable))


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(path.name + f".bak-sgme-{stamp}")
    shutil.copy2(path, target)
    return target


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise TypeError(f"{path} is not a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def register_mcp() -> None:
    settings = Settings.from_env()
    entry = {
        "type": "stdio",
        "command": _venv_python(),
        "args": ["-m", "claude_sgme.mcp_proxy"],
        "env": {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "SGME_AGENT_ID": settings.agent_id,
            "SGME_BASE_URL": settings.base_url,
            "SGME_MCP_URL": settings.mcp_url,
        },
        # 密钥走环境透传，不写入 JSON；config.py 另有 ~/.sgme/claude-agent.json 兜底
        "env_vars": ["SGME_CLAUDE_KEY"],
        "startup_timeout_sec": 30,
    }
    data = _read_json(CLAUDE_JSON)
    servers = data.setdefault("mcpServers", {})
    backup = _backup(CLAUDE_JSON)
    previous = servers.get("sgme")
    servers["sgme"] = entry
    _write_json(CLAUDE_JSON, data)
    print(f"✓ mcpServers.sgme {'updated' if previous else 'added'} -> {entry['command']}")
    if backup:
        print(f"  backup: {backup}")


def _is_our_hook_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", ""))
        for hook in hooks
    )


def register_hooks() -> None:
    python = _venv_python()
    data = _read_json(SETTINGS_JSON)
    hooks_section = data.setdefault("hooks", {})
    backup = _backup(SETTINGS_JSON)
    for event, subcommand in HOOK_EVENTS:
        entries = hooks_section.setdefault(event, [])
        if not isinstance(entries, list):
            raise TypeError(f"hooks.{event} is not a list")
        entries[:] = [entry for entry in entries if not _is_our_hook_entry(entry)]
        entries.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f'"{python}" -m claude_sgme.cli hook {subcommand}',
                    }
                ]
            }
        )
        print(f"✓ hooks.{event} registered")
    _write_json(SETTINGS_JSON, data)
    if backup:
        print(f"  backup: {backup}")


def check() -> int:
    entry = (_read_json(CLAUDE_JSON).get("mcpServers") or {}).get("sgme") or {}
    ok_mcp = "claude_sgme.mcp_proxy" in str(entry.get("args"))
    hooks_section = _read_json(SETTINGS_JSON).get("hooks") or {}
    ok_hooks = True
    for event, _ in HOOK_EVENTS:
        entries = hooks_section.get(event)
        if not isinstance(entries, list) or not any(_is_our_hook_entry(e) for e in entries):
            ok_hooks = False
    print(f"mcp: {'ok' if ok_mcp else 'missing'}  hooks: {'ok' if ok_hooks else 'missing'}")
    return 0 if (ok_mcp and ok_hooks) else 1


def _configure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        rc = getattr(stream, "reconfigure", None)
        if callable(rc):
            try:
                rc(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    _configure_utf8()
    parser = argparse.ArgumentParser(description="Install the Claude Code x SGME adapter")
    parser.add_argument("--mcp", action="store_true", help="只注册 MCP server")
    parser.add_argument("--hooks", action="store_true", help="只注册 hooks")
    parser.add_argument("--check", action="store_true", help="只检查注册状态")
    args = parser.parse_args()

    if args.check:
        return check()

    do_mcp = args.mcp or not args.hooks
    do_hooks = args.hooks or not args.mcp
    if do_mcp:
        register_mcp()
    if do_hooks:
        register_hooks()
    print("完成。重启 Claude Code 后生效（新会话自动：注入画像 / 每轮落盘 / 收尾提炼）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
