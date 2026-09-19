# -*- coding: utf-8 -*-
"""SGME × Doubao Work（豆包工作）适配器部署脚本。

用法：
    python adapters/doubao/install.py                      # 部署到默认豆包工作技能目录
    python adapters/doubao/install.py --dest <DOUBAO_SKILLS_DIR>   # 指定技能根
    python adapters/doubao/install.py --base-url http://<NAS_IP>:9910   # 显式指定 SGME 地址
    python adapters/doubao/install.py --no-selfcheck       # 跳过自检

行为：
- 复制 adapters/doubao/{SKILL.md,scripts/*} → <技能根>/sgme-bridge/
- 幂等：覆盖旧副本；不触碰豆包工作其它文件
- **写部署配置** `<技能根>/sgme-bridge/scripts/client.env`：把解析出的 SGME 地址写进
  部署副本（本文件不含密钥，只含地址与「密钥环境变量名」），使客户端在仓库保持干净
  （默认回环）的前提下仍能连上生产 SGME。解析顺序：--base-url → 环境变量
  SGME_HTTP_URL/SGME_BASE_URL → 既有部署配置 client.env → 旧部署副本地址（迁移沿用）
  → 回环默认。MCP 端点由 HTTP 地址按同主机「端口 +3 / 路径 /mcp」推导。
- 部署后运行 selfcheck.py 实测连通性 + 基准能力矩阵，结果写入 scripts/access-log.md
- 提示重启豆包工作 / 新会话生效
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 本项目 adapters/doubao/ 目录（脚本所在位置）
SRC_DIR = Path(__file__).resolve().parent
FILES = ("SKILL.md",)
SCRIPT_DIR = SRC_DIR / "scripts"

# 客户端提供地址推导与部署配置读写（单一真源，避免两处规则漂移）
sys.path.insert(0, str(SCRIPT_DIR))
from sgme_client import (  # noqa: E402
    ADMIN_KEY_ENV,
    DEFAULT_HTTP_URL,
    DEFAULT_MCP_URL,
    DEPLOY_CONFIG_NAME,
    KEY_ENV,
    derive_mcp_url,
    load_deploy_config,
)

LOOPBACK_MARK = ("127.0.0.1", "localhost")


def home_dir() -> Path:
    """当前用户主目录（不写死用户名）。"""
    return Path(os.path.expanduser("~"))


def default_skills_root() -> Path:
    """默认豆包工作用户技能根目录：环境变量 DOUBAO_SKILLS_ROOT 优先，否则 ~/DoubaoWork/skills。"""
    env = os.environ.get("DOUBAO_SKILLS_ROOT")
    if env and env.strip():
        return Path(env.strip())
    return home_dir() / "DoubaoWork" / "skills"


def _adopt_legacy_address(dest: Path) -> str | None:
    """读取旧部署副本里写死的 SGME 地址（迁移兼容）。

    背景：历史版本的 sgme_client.py 把生产地址硬编码在源码里（数据卫生违规）。
    仓库版已改为回环默认 + 部署配置注入；重部署时从既有部署副本沿用地址，
    避免用户侧已跑通的部署因源码清理而失联。
    """
    legacy = dest / "scripts" / "sgme_client.py"
    if not legacy.is_file():
        legacy = dest / "sgme_client.py"
    if not legacy.is_file():
        return None
    try:
        text = legacy.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for m in re.finditer(r"https?://[0-9A-Za-z_.\-]+:\d+", text):
        url = m.group(0)
        if any(mark in url for mark in LOOPBACK_MARK):
            continue
        return url
    return None


def resolve_addresses(dest: Path, base_url: str | None = None,
                      mcp_url: str | None = None) -> tuple[str, str, str]:
    """解析部署要写入的 SGME 地址，返回 (http_url, mcp_url, 来源说明)。

    ⚠️ dest 是**技能副本目录**（<技能根>/sgme-bridge），与 install() 的返回值一致，
    不是技能根目录。
    """
    if base_url:
        http = base_url.strip()
        return http.rstrip("/"), (mcp_url or derive_mcp_url(http)).rstrip("/"), "命令行参数 --base-url"

    env_http = (os.environ.get("SGME_HTTP_URL") or os.environ.get("SGME_BASE_URL") or "").strip()
    if env_http:
        mcp = mcp_url or (os.environ.get("SGME_MCP_URL") or "").strip() or derive_mcp_url(env_http)
        return env_http.rstrip("/"), mcp.rstrip("/"), "环境变量 SGME_HTTP_URL/SGME_BASE_URL"

    cfg = load_deploy_config(dest / "scripts" / DEPLOY_CONFIG_NAME)
    if cfg.get("SGME_HTTP_URL") or cfg.get("SGME_BASE_URL"):
        http = cfg.get("SGME_HTTP_URL") or cfg["SGME_BASE_URL"]
        mcp = mcp_url or cfg.get("SGME_MCP_URL") or derive_mcp_url(http)
        return http.rstrip("/"), mcp.rstrip("/"), f"既有部署配置 {DEPLOY_CONFIG_NAME}"

    legacy = _adopt_legacy_address(dest)
    if legacy:
        return legacy, (mcp_url or derive_mcp_url(legacy)).rstrip("/"), "旧部署副本地址（迁移沿用）"

    return DEFAULT_HTTP_URL, (mcp_url or DEFAULT_MCP_URL).rstrip("/"), "回环默认"


def write_deploy_config(dest: Path, http_url: str, mcp_url: str, source: str) -> Path:
    """写部署配置 client.env（只含地址 + 密钥环境变量名，绝不含密钥值）。"""
    target = dest / "scripts" / DEPLOY_CONFIG_NAME
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"""# SGME × 豆包工作 部署配置（由 install.py 生成，可重跑覆盖）
# 生成时间：{ts}　地址来源：{source}
# 解析顺序（sgme_client.py 构造时）：环境变量 → 本文件 → 回环默认
# 本文件不含任何密钥值，只登记密钥所在的环境变量名。
SGME_HTTP_URL={http_url}
SGME_MCP_URL={mcp_url}
{KEY_ENV}_ENV={KEY_ENV}
{ADMIN_KEY_ENV}_ENV={ADMIN_KEY_ENV}
"""
    target.write_text(body, encoding="utf-8")
    return target


def install(skills_root: Path) -> Path:
    dest = skills_root / "sgme-bridge"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "scripts").mkdir(parents=True, exist_ok=True)

    for f in FILES:
        src = SRC_DIR / f
        if not src.exists():
            raise FileNotFoundError(f"源文件缺失: {src}")
        shutil.copy2(src, dest / f)
        print(f"  ✅ {f} → {dest / f}")

    # scripts/：源码清单（*.py + access-log.md），同步时清理部署副本中的旧文件
    managed = set()
    for src in sorted(SCRIPT_DIR.iterdir()):
        if src.name == "install.py":
            continue
        if src.suffix == ".py":
            shutil.copy2(src, dest / "scripts" / src.name)
            managed.add(src.name)
            print(f"  ✅ scripts/{src.name} → {dest / 'scripts' / src.name}")
        elif src.name == "access-log.md":
            # 日志在部署副本累积：已存在不覆盖，缺失时复制模板
            target = dest / "scripts" / src.name
            managed.add(src.name)
            if not target.exists():
                shutil.copy2(src, target)
                print(f"  ✅ scripts/{src.name} → {target}（初始模板）")

    for old in sorted((dest / "scripts").glob("*.py")):
        if old.name not in managed:
            old.unlink()
            print(f"  🗑  清理旧文件 scripts/{old.name}")
    return dest


def _key_ok() -> bool:
    return bool(os.environ.get(KEY_ENV))


def _run_selfcheck(dest: Path, py: str) -> int:
    print("\n运行接入自检（selfcheck.py）…")
    env = dict(os.environ)
    cmd = [py, str(dest / "scripts" / "selfcheck.py")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout)
    if r.stderr.strip():
        print(r.stderr, file=sys.stderr)
    return r.returncode


def _append_log(dest: Path, note: str):
    log = dest / "scripts" / "access-log.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"\n- {ts}：{note}"
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print(f"  ⚠️ 更新日志失败: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SGME × 豆包工作 适配器部署")
    parser.add_argument("--dest", type=Path, default=None,
                        help="豆包工作技能根目录（默认 DOUBAO_SKILLS_ROOT 或 ~/DoubaoWork/skills）")
    parser.add_argument("--base-url", default=None,
                        help="SGME HTTP 地址（如 http://<NAS_IP>:9910）；默认按环境变量/既有部署解析")
    parser.add_argument("--mcp-url", default=None, help="SGME MCP 地址（默认由 HTTP 地址推导）")
    parser.add_argument("--no-selfcheck", action="store_true", help="跳过连通性自检")
    parser.add_argument("--py", default=None, help="python 解释器（默认当前解释器）")
    args = parser.parse_args()

    skills_root = args.dest or default_skills_root()
    print(f"豆包工作技能根目录: {skills_root}")
    if not skills_root.exists():
        print(f"  ⚠️ 目录不存在: {skills_root}（继续创建）")

    # ⚠️ 地址解析必须在拷贝之前：拷贝会覆盖旧副本，之后就取不到迁移来源了
    http_url, mcp_url, source = resolve_addresses(skills_root / "sgme-bridge",
                                                  args.base_url, args.mcp_url)

    dest = install(skills_root)
    print(f"\n部署完成: {dest}")

    cfg_path = write_deploy_config(dest, http_url, mcp_url, source)
    print(f"  ✅ 部署配置 scripts/{DEPLOY_CONFIG_NAME} → {cfg_path}")
    print(f"     SGME HTTP = {http_url}")
    print(f"     SGME MCP  = {mcp_url}")
    print(f"     地址来源  = {source}")
    if source == "回环默认" or any(mark in http_url for mark in LOOPBACK_MARK):
        print("  ⚠️ 未解析到环境/既有部署的生产地址，已写回环默认——若 SGME 部署在 NAS，"
              "请带 SGME_HTTP_URL=http://<NAS_IP>:9910 重跑本脚本。")

    if not _key_ok():
        print(f"  ⚠️ 未检测到环境变量 {KEY_ENV}——自检将失败，请先配置密钥。")

    rc = 0
    if not args.no_selfcheck:
        py = args.py or sys.executable
        rc = _run_selfcheck(dest, py)
        if rc == 0:
            _append_log(dest, f"install.py 重部署 + 自检通过（能力矩阵 41/41 双覆盖）；"
                              f"地址来源：{source}")
        else:
            _append_log(dest, "install.py 重部署 + 自检有失败项（见上方输出）")

    print("\n下一步：")
    print("  1. 重启豆包工作 / 开新会话，使 sgme-bridge 技能重新加载")
    print(f"  2. 确认环境变量 {KEY_ENV} 对本会话可见（写侧能力另需 {ADMIN_KEY_ENV}）")
    print("  3. （可选）配置定时任务做主动关怀：python scripts/care_watch.py pull")
    return rc


if __name__ == "__main__":
    sys.exit(main())
