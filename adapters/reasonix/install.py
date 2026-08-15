"""install.py — Reasonix × SGME 适配器一键安装。

功能：
1. 生成项目级 `.reasonix/settings.json`（SessionStart 注入 + SessionEnd 捕获 hooks）
2. 注册 agent_id=reasonix（SGME /v1/admin/agents/register），key 存 adapters/reasonix/.env
3. 打印接入说明

用法：
  <项目根>/.venv/Scripts/python.exe <项目根>/adapters/reasonix/install.py --dir <目标项目>
  --no-register  跳过 agent 注册（已有 key 时）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

logger = logging.getLogger("sgme.reasonix.install")

# 项目根（本文件位于 <root>/adapters/reasonix/install.py）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
BRIDGE = PROJECT_ROOT / "adapters" / "reasonix" / "bridge.py"
ENV_FILE = Path(__file__).resolve().parent / ".env"

_BASE_URL = os.environ.get("SGME_BASE_URL", "http://192.168.10.10:9910").rstrip("/")
_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")
_AGENT_ID = os.environ.get("SGME_REASONIX_AGENT_ID", "reasonix")


# ---------- settings.json 模板 ----------

AGENTS_MD_TEMPLATE = """# SGME 记忆系统（本项目已接入）

你的会话会被自动记录并提炼为跨 Agent 共享的长期记忆（SGME 引擎）。
规则（**必须遵守**）：

1. **会话自动记录**：会话结束自动入库并提炼，无需你做任何事；涉及重要决策/事实时，尽量表达清楚完整（会被提炼成记忆）。
2. **强制查询**：凡涉及用户或项目的**历史事实**问题（之前/以前/上次/当初/还记得…），**必须先运行 `/sgme <关键词>` 查询再回答**——不得直接说"不知道"。查询不到时如实说明"记忆库中未找到"。
3. **画像注入**：会话开始时已注入用户画像与相关记忆（若存在），可直接引用，不必重复询问用户；画像中没有的信息按规则 2 查询。
4. **历史会话补导入**：本适配器提供历史会话全量导入方法（把接入前的存量会话补进 SGME）：
   `{python} {import_history}`
   幂等可重跑（已导入的自动跳过）。需要补录历史时执行它即可，然后汇报导入数量。

命令 `/sgme` 由 `.reasonix/commands/sgme.md` 提供（执行桥接脚本调 SGME 检索接口）。

> 注：以上路径为本机安装时生成；仓库迁移/克隆到其他机器后，重跑 install.py 即可刷新为本机路径。
"""

SGME_COMMAND_TEMPLATE = """---
description: 查询 SGME 长期记忆（跨 Agent 共享）
argument-hint: <关键词>
---
运行以下命令检索 SGME 记忆，把结果作为参考上下文：

  {python} {bridge} --query "$ARGUMENTS"

将返回的记忆内容整理进你的回答（标注来源层），未找到时如实说明。
"""


def generate_agents_md() -> str:
    """生成 AGENTS.md（SGME 使用声明——让 Reasonix 模型**知道**记忆系统存在）。"""
    return AGENTS_MD_TEMPLATE.format(
        python=str(PYTHON).replace("\\", "/"),
        import_history=str(PROJECT_ROOT / "adapters" / "reasonix" / "import_history.py").replace("\\", "/"),
    )


def generate_sgme_command() -> str:
    """生成 .reasonix/commands/sgme.md（/sgme 查询命令）。"""
    return SGME_COMMAND_TEMPLATE.format(
        python=str(PYTHON).replace("\\", "/"),
        bridge=str(BRIDGE).replace("\\", "/"),
    )


def write_agents_md(project_dir: Path) -> Path:
    """写入项目 AGENTS.md（已存在时保留用户内容并追加/升级 SGME 段）。"""
    target = Path(project_dir) / "AGENTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        text = target.read_text(encoding="utf-8")
        if "SGME 记忆系统" not in text:
            # 用户自建文件：追加 SGME 段，不覆盖
            target.write_text(text.rstrip() + "\n---\n\n" + generate_agents_md(), encoding="utf-8")
        elif "历史会话补导入" not in text:
            # 旧版模板（规则不全）：整体升级——旧版整个文件都是本模板生成，无用户内容
            target.write_text(generate_agents_md(), encoding="utf-8")
        return target
    target.write_text(generate_agents_md(), encoding="utf-8")
    return target


def write_sgme_command(project_dir: Path) -> Path:
    """写入 .reasonix/commands/sgme.md（/sgme 命令）。"""
    target = Path(project_dir) / ".reasonix" / "commands" / "sgme.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate_sgme_command(), encoding="utf-8")
    return target

def generate_settings_json() -> str:
    """生成 .reasonix/settings.json 内容（hook 命令用绝对路径，不依赖 shell PATH）。"""
    bridge_posix = str(BRIDGE).replace("\\", "/")
    python_posix = str(PYTHON).replace("\\", "/")
    data = {
        "hooks": {
            "SessionStart": [
                {
                    "command": f"{python_posix} {bridge_posix} --start",
                    "description": "SGME 记忆注入（画像 + 项目相关记忆）",
                }
            ],
            "SessionEnd": [
                {
                    "command": f"{python_posix} {bridge_posix} --end",
                    "description": "SGME 会话捕获（L0 写入 + 提炼触发）",
                }
            ],
        }
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def write_settings(project_dir: Path) -> Path:
    """写入项目 .reasonix/settings.json，返回路径。"""
    target = Path(project_dir) / ".reasonix" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate_settings_json() + "\n", encoding="utf-8")
    return target


# ---------- agent 注册 ----------

def _http() -> httpx.Client | None:
    if httpx is None:
        return None
    return httpx.Client(timeout=5.0, trust_env=False)


def register_agent() -> str | None:
    """注册 agent_id=reasonix，返回明文 key（仅此一次显示）。失败返回 None。"""
    cli = _http()
    if cli is None:
        return None
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/admin/agents/register",
            json={"agent_id": _AGENT_ID, "scope": ["memory:rw"]},
            headers={"X-API-Key": _ADMIN_KEY},
        )
        if r.status_code != 200:
            logger.warning("agent 注册失败: %s %s", r.status_code, r.text[:200])
            return None
        return r.json().get("api_key")
    except Exception as e:
        logger.warning("agent 注册异常: %s", e)
        return None
    finally:
        cli.close()


def save_key(key: str) -> Path:
    """key 写入 adapters/reasonix/.env（bridge.py 启动时加载，不硬编码进代码）。"""
    lines = []
    if ENV_FILE.exists():
        lines = [l for l in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("SGME_AGENT_KEY")]
    lines.append(f"SGME_AGENT_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ENV_FILE


# ---------- 入口 ----------

def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Reasonix × SGME 适配器安装")
    parser.add_argument("--dir", required=True, help="目标项目目录（生成其 .reasonix/settings.json）")
    parser.add_argument("--no-register", action="store_true", help="跳过 agent 注册")
    args = parser.parse_args(argv)

    settings_path = write_settings(Path(args.dir))
    print(f"[1/4] hooks 配置已生成: {settings_path}")
    agents_path = write_agents_md(Path(args.dir))
    print(f"[2/4] AGENTS.md 声明已写入: {agents_path}（模型启动必加载，知悉记忆系统）")
    cmd_path = write_sgme_command(Path(args.dir))
    print(f"[3/4] /sgme 查询命令已写入: {cmd_path}")

    key = None
    if not args.no_register:
        key = register_agent()
        if key:
            env_path = save_key(key)
            print(f"[4/4] agent_id={_AGENT_ID} 已注册，key 已存: {env_path}（仅本地文件，勿外传）")
        else:
            print("[4/4] agent 注册失败（Gateway 未启动或 admin key 不对），跳过；"
                  "bridge.py 将使用默认 agent key")
    else:
        print("[4/4] 已跳过 agent 注册")

    print("完成。重启 Reasonix（或 /reload）使 hooks 生效。")
    print()
    print("验证：")
    print(f"  reasonix hook list --json --dir {args.dir}")
    print("  （应看到 SessionStart/SessionEnd 两条 active）")
    print(f"  /sgme 测试：reasonix run --dir {args.dir} -p \"/sgme 测试\"")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
