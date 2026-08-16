"""install.py — DeepSeek Harness (dsh) × SGME 适配器一键安装。

与 reasonix install.py 对齐的瘦安装引导（运行时逻辑全在 sgme-bridge TS 插件内，
本脚本不含运行时桥接）。

功能：
1. 注册 agent_id=dsh（SGME /v1/admin/agents/register），key 存 adapters/dsh/.env
2. 写入目标项目 AGENTS.md 的 SGME 声明段（让 dsh 模型知悉记忆系统）
3. 打印 dsh 插件加载命令（本地 link 模式，改代码即生效）

用法：
  <项目根>/.venv/Scripts/python.exe <项目根>/adapters/dsh/install.py --dir <目标项目>
  --no-register  跳过 agent 注册（已有 key 时）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

logger = logging.getLogger("sgme.dsh.install")

# 项目根（本文件位于 <root>/adapters/dsh/install.py）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
# TS 插件本体目录（标准 dsh 插件结构）
SGME_BRIDGE_DIR = PROJECT_ROOT / "adapters" / "dsh" / "sgme-bridge"
# 适配器自身 .env（import_history.py 等 Python 侧使用）
ENV_FILE = Path(__file__).resolve().parent / ".env"
# dsh 可加载的 .env（dsh 从启动目录 <cwd>/.env 或 $DSH_HOME/.env 物化进 process.env）。
# 默认 SGME 项目根（用户在项目根启动 dsh 最常见）；可用 --dsh-env 覆盖。
DSH_ENV_FILE = PROJECT_ROOT / ".env"

_BASE_URL = os.environ.get("SGME_BASE_URL", "http://192.168.10.10:9910").rstrip("/")
_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")
_AGENT_ID = os.environ.get("SGME_DSH_AGENT_ID", "dsh")
# T-43 动态链声明：dsh 提炼跟随的模型（provider/model 格式）。注册时落 agent_keys.json，
# append 未显式传 agent_model 时按 agent_id 反查落 raw_files.agent_model，提炼据此跟随。
_AGENT_MODEL = os.environ.get("SGME_DSH_AGENT_MODEL", "deepseek/deepseek-v4-flash")


# ---------- AGENTS.md 模板 ----------

AGENTS_MD_TEMPLATE = """# SGME 记忆系统（本项目已接入）

你的会话会被自动记录并提炼为跨 Agent 共享的长期记忆（SGME 引擎）。
规则（**必须遵守**）：

1. **会话自动记录**：每个对话回合结束自动入库并提炼，无需你做任何事；涉及重要决策/事实时，尽量表达清楚完整（会被提炼成记忆）。
2. **强制查询**：凡涉及用户或项目的**历史事实**问题（之前/以前/上次/当初/还记得…），**必须先用 `memory_search` 工具或 `/sgme <关键词>` 命令查询再回答**——不得直接说"不知道"。查询不到时如实说明"记忆库中未找到"。
3. **画像注入**：会话开始时已注入用户画像与相关记忆（若存在），可直接引用，不必重复询问用户；画像中没有的信息按规则 2 查询。
4. **历史会话补导入**：本适配器提供历史会话全量导入方法（把接入前的存量会话补进 SGME）：
   `{python} {import_history}`
   幂等可重跑（已导入的自动跳过）。需要补录历史时执行它即可，然后汇报导入数量。
5. **可用工具**：
   - `memory_search`：检索 SGME 长期记忆（L1.5 记忆池）
   - `wiki_search`：检索 SGME 知识库（L2 场景 + wiki_pages）
   - `/sgme <关键词>`：综合检索命令（memory + wiki）

> 注：以上路径为本机安装时生成；仓库迁移/克隆到其他机器后，重跑 install.py 即可刷新为本机路径。
"""


def generate_agents_md() -> str:
    """生成 AGENTS.md（SGME 使用声明——让 dsh 模型**知道**记忆系统存在）。"""
    return AGENTS_MD_TEMPLATE.format(
        python=str(PYTHON).replace("\\", "/"),
        import_history=str(PROJECT_ROOT / "adapters" / "dsh" / "import_history.py").replace("\\", "/"),
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
            # 旧版模板（规则不全）：整体升级
            target.write_text(generate_agents_md(), encoding="utf-8")
        return target
    target.write_text(generate_agents_md(), encoding="utf-8")
    return target


# ---------- 索引 skill 部署（W6，方案 v0.3 §5.6） ----------

# 源 skill（随 git 管理，部署副本可重建）
SKILL_SRC = Path(__file__).resolve().parent / "skills" / "wiki-skill-discovery" / "SKILL.md"


def default_skills_dir() -> Path:
    """消费端技能根（DSH agentsHome skills；可用环境变量覆盖）。"""
    env = os.environ.get("DSH_SKILLS_DIR", "")
    return Path(env) if env else Path.home() / ".agents" / "skills"


def deploy_index_skill(skills_dir: Path) -> Path | None:
    """部署 wiki-skill-discovery 到消费端 skills 目录（可重建的部署副本）。

    幂等：目标已存在且内容一致 → 跳过；不一致 → 覆盖更新（源在 git，重跑即重建）。
    返回部署目标路径；源缺失时返回 None。
    """
    if not SKILL_SRC.exists():
        logger.warning("索引 skill 源缺失: %s", SKILL_SRC)
        return None
    target = skills_dir / "wiki-skill-discovery" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    src_text = SKILL_SRC.read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") == src_text:
        logger.info("索引 skill 已部署且一致（跳过）: %s", target)
        return target
    target.write_text(src_text, encoding="utf-8")
    logger.info("索引 skill 已部署: %s", target)
    return target


# ---------- agent 注册 ----------

def _http() -> httpx.Client | None:
    if httpx is None:
        return None
    return httpx.Client(timeout=5.0, trust_env=False)  # trust_env=False 防 Clash 劫持 localhost


def register_agent() -> str | None:
    """注册 agent_id=dsh，返回明文 key（仅此一次显示）。失败返回 None。

    声明 agent_model（T-43 动态链）：提炼跟随 dsh 当前 LLM，用户指定
    refine.llm_override 后此值降为备用。
    """
    cli = _http()
    if cli is None:
        return None
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/admin/agents/register",
            json={"agent_id": _AGENT_ID, "scope": ["memory:rw"], "agent_model": _AGENT_MODEL},
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


def save_key(key: str, env_file: Path | None = None) -> Path:
    """key 写入 .env（install.py 写入，sgme-bridge TS 插件运行时读）。

    与 reasonix 同款：保留已有非 SGME_AGENT_KEY 行，追加新 key。
    env_file 缺省写 ENV_FILE（adapters/dsh/.env）；也可指定 dsh 加载路径。
    """
    env_file = env_file or ENV_FILE
    lines = []
    if env_file.exists():
        lines = [l for l in env_file.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("SGME_AGENT_KEY")
                 and not l.startswith("SGME_ADMIN_KEY")
                 and not l.startswith("SGME_BASE_URL")]
    lines.append(f"SGME_BASE_URL={_BASE_URL}")
    lines.append(f"SGME_AGENT_KEY={key}")
    # admin key 也写入（触发提炼用，import_history.py 需要）
    admin_key = os.environ.get("SGME_ADMIN_KEY", "")
    if admin_key:
        lines.append(f"SGME_ADMIN_KEY={admin_key}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_file


# ---------- 入口 ----------

def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="DeepSeek Harness × SGME 适配器安装")
    parser.add_argument("--dir", required=True, help="目标项目目录（生成其 AGENTS.md）")
    parser.add_argument("--no-register", action="store_true", help="跳过 agent 注册")
    parser.add_argument("--skills-dir", default=str(default_skills_dir()),
                        help="消费端 skills 目录（部署索引 skill，默认 ~/.agents/skills）")
    parser.add_argument("--dsh-env", default=str(DSH_ENV_FILE),
                        help="dsh 可加载的 .env 路径（dsh 从启动目录 <cwd>/.env 或 $DSH_HOME/.env 读取），"
                             "默认 SGME 项目根 .env")
    args = parser.parse_args(argv)

    print("[1/3] 注册 agent + 写入 .env")
    key = None
    if not args.no_register:
        key = register_agent()
        if key:
            # 写两处：adapters/dsh/.env（Python 侧用）+ dsh 加载路径（插件用）
            adapters_env = save_key(key)
            dsh_env = save_key(key, Path(args.dsh_env))
            print(f"  agent_id={_AGENT_ID} 已注册，key 已存: {adapters_env}（仅本地文件，勿外传）")
            print(f"  dsh 加载 .env: {dsh_env}（dsh 启动目录需含此文件，插件据此读 key）")
        else:
            print("  ⚠️ agent 注册失败（Gateway 未启动或 admin key 不对），跳过；"
                  "sgme-bridge 将使用默认 agent key")
    else:
        print("  已跳过 agent 注册")

    print("[2/3] 写入 AGENTS.md 声明")
    agents_path = write_agents_md(Path(args.dir))
    print(f"  {agents_path}（模型启动必加载，知悉记忆系统）")

    print("[3/4] 部署索引 skill（wiki-skill-discovery）")
    deployed = deploy_index_skill(Path(args.skills_dir))
    if deployed:
        print(f"  {deployed}（源在 adapters/dsh/skills/，随 git 管理，重跑本脚本可重建）")
    else:
        print("  ⚠️ 索引 skill 源缺失，部署跳过")

    print("[4/4] dsh 插件加载命令（请手动执行确认环境）")
    bridge_posix = str(SGME_BRIDGE_DIR).replace("\\", "/")
    print(f"  # 本地开发（link 模式，改代码即生效）")
    print(f'  dsh plugin --profile web add "link:{bridge_posix}"')
    print(f"  dsh --profile web")
    print()
    print("验证：")
    print(f"  dsh --profile web --dump-config    # 确认 dsh-sgme 插件已挂载")
    print(f"  /sgme 测试                          # 会话内验证检索")
    print(f"  GET /v1/admin/sessions              # 查 SGME 确认 L0 入库")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
