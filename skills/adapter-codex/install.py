from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from codex_sgme import __version__
from codex_sgme.config import Settings


def build_manifest() -> dict[str, Any]:
    return {
        "adapter": "codex-sgme",
        "version": __version__,
        "lifecycle": {
            "session_start": "start",
            "turn_end": "turn",
            "session_end": "end",
        },
        "commands": {
            "start": [
                "python",
                "-m",
                "codex_sgme.cli",
                "start",
                "--session-key",
                "<session_key>",
                "--cursor-file",
                "<cursor_file>",
            ],
            "turn": [
                "python",
                "-m",
                "codex_sgme.cli",
                "turn",
                "--session-key",
                "<session_key>",
                "--cursor-file",
                "<cursor_file>",
            ],
            "end": [
                "python",
                "-m",
                "codex_sgme.cli",
                "end",
                "--session-key",
                "<session_key>",
                "--cursor-file",
                "<cursor_file>",
            ],
        },
        "stdin_protocol": {
            "turn": {
                "started_at": "ISO-8601 timestamp (optional)",
                "messages": "array of role/content objects",
            }
        },
        "notes": [
            "The manifest is host-neutral and does not modify global Codex settings.",
            "The host must provide SGME connection variables in its process environment.",
        ],
    }


def write_manifest(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _proxy_command(project_dir: Path) -> list[str]:
    project_python = project_dir / ".venv" / "Scripts" / "python.exe"
    python = project_python if project_python.exists() else Path(sys.executable)
    return [str(python), "-m", "codex_sgme.mcp_proxy"]


def register_codex_mcp(
    *,
    codex_executable: str,
    project_dir: Path,
    base_url: str,
    mcp_url: str,
    runner=subprocess.run,
) -> None:
    command = [
        codex_executable,
        "mcp",
        "add",
        "sgme",
        "--env",
        f"SGME_BASE_URL={base_url}",
        "--env",
        f"SGME_MCP_URL={mcp_url}",
        "--env",
        "SGME_AGENT_ID=codex",
        "--env",
        "PYTHONUTF8=1",
        "--env",
        "PYTHONIOENCODING=utf-8",
        "--",
        *_proxy_command(project_dir),
    ]
    runner(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a Codex-SGME integration manifest"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--codex-executable", default=None)
    args = parser.parse_args()
    settings = Settings.from_env()
    if not args.output and not args.register:
        parser.error("at least one of --output or --register is required")
    if args.output:
        write_manifest(args.output)
        print(args.output)
    if args.register:
        codex = args.codex_executable or shutil.which("codex")
        if not codex:
            parser.error("codex executable not found; pass --codex-executable")
        register_codex_mcp(
            codex_executable=codex,
            project_dir=Path(__file__).resolve().parent,
            base_url=settings.base_url,
            mcp_url=settings.mcp_url,
        )
        print("registered sgme MCP server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
