#!/usr/bin/env python3
"""project_init.py — 项目立项一键初始化（2026-08-09 用户定流程）

立项清单（六步，不可跳过）：
  ① 创建项目文件夹 <项目目录>\\<name>
  ② 生成初始 Backlog 骨架（docs/requirements/<name>-Backlog-v0.1.md）
  ③ 生成设计文档占位（docs/design/README.md）
  ④ 登记 SGME 项目注册（/v1/append，projects 维度，带溯源）
  ⑤ 需求池关联：相关需求标记"已立项"（如果有的话）
  ⑥ git init + 首次 commit

用法：
  python project_init.py <项目名> [--desc "描述"] [--dir <项目目录>]

触发条件：用户明确说"立项/开搞/新建项目"（不是聊聊）。
防遗忘：SOUL.md 铁律 + cron 审计（扫描未登记项目目录）。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECTS_ROOT = Path(os.environ.get("SGME_PROJECTS_ROOT") or os.getcwd()).resolve()
SGME_URL = "http://localhost:9910/v1/append"
SGME_KEY = os.environ.get("SGME_AGENT_KEY", "") or (
    dict(
        l.strip().split("=", 1)
        for l in (Path(__file__).resolve().parents[1] / "config" / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in l and not l.strip().startswith("#")
    ).get("SGME_AGENT_KEY", "dev-agent-key-change-me")
)
SGME_AGENT_ID = "default"

BACKLOG_TEMPLATE = """# {name} Backlog v0.1

> 锚文档（Issue-Driven Development）：立项即有、持续存在、不删除。
> 术语对齐 GitHub Issue 体系：Epic → Story → Task/Bug；全部 Task 满足 AC = Story ✅

## Epic

| 编号 | 类型 | 标题 | 状态 | 版本 | AC |
|------|------|------|------|------|----|
| EP-1 | Epic | {name} 核心 | 🔴 未解决 | — | — |

## Story 池

| 编号 | 类型 | 标题 | 状态 | 版本 | AC |
|------|------|------|------|------|----|
| ST-1 | Story | 待拆解 | 🔴 未解决 | — | — |

## Task / Bug 池

| 编号 | 类型 | 标题 | 归属 Story | 状态 | 版本 | 备注 |
|------|------|------|-----------|------|------|------|
| T-1 | Task | 待拆解 | ST-1 | 🔴 未解决 | — | — |

## 设计文档索引

| 文档 | 解决需求 | 状态 |
|------|---------|------|
| docs/design/（待创建） | — | 🔴 |
"""

DESIGN_README = """# 设计文档

> 设计文档依附于 Backlog：每个设计文档声明解决哪些 Story/Task。
> 版本惯例：原版本存档、新版本递增（0.1 → 0.1.1 → 0.2）。

## 目录

| 文档 | 解决需求 | 状态 |
|------|---------|------|
| （待创建） | — | 🔴 |
"""


def register_sgme(name: str, path: str, desc: str) -> bool:
    """登记 SGME 项目注册（projects 维度记忆，带溯源）。"""
    text = (
        f"# {datetime.now().isoformat()} user\n"
        f"项目立项：{name}，路径 {path}，{desc or '暂无描述'}。"
        "登记于项目注册表（project_meta 雏形，ST-16 落地后迁移）。\n"
        f"# {datetime.now().isoformat()} assistant\n项目注册信息已记录，projects 维度。"
    )
    payload = {
        "session_key": f"project-init-{name}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "agent_id": SGME_AGENT_ID,
        "started_at": datetime.now().isoformat(),
        "content": text,
    }
    try:
        req = urllib.request.Request(
            SGME_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": SGME_KEY},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(f"  ✅ SGME 登记完成: {data.get('status', 'ok')}")
        return True
    except Exception as e:
        print(f"  ⚠️ SGME 登记失败（不影响立项，稍后补登）: {e}")
        return False


# ---------- 0.8 ST-16：项目注册表登记（project_meta） ----------
#   register_sgme        → /v1/append，进**记忆池**（projects 维度会话记忆，带溯源）
#   register_project_meta → /v1/admin/projects，进**项目注册表**（结构化元数据，可列表/检索）
SGME_PROJECTS_URL = "http://localhost:9910/v1/admin/projects"


def register_project_meta(name: str, path: str) -> bool:
    """登记项目注册表 project_meta（ST-16，POST /v1/admin/projects，upsert 幂等）。

    🔴 密钥纪律：admin key **只从环境变量 SGME_ADMIN_KEY 读，无默认值、不硬编码**。
    未设置时跳过登记并提示（提示中不打印 key 本身）。
    注意本文件第 29 行 SGME_KEY 的 "dev-agent-key-change-me" 默认值是基线遗留写法，
    新代码不沿用——admin 端点权限更高，落个默认占位符等于把口子留在仓库里。

    失败不阻断立项：沿用 ④ 步既有设计意图（登记失败只警告、稍后补登）。

    不传 desc：project_meta 是轻量元数据表（项目名/路径/git 仓库/最近活跃/里程碑），
    无描述列；项目描述已由 register_sgme() 写进记忆池，无需重复。
    不传 git_repo：⑥ 步 git init 尚未执行，此处留空，后续由
    `PATCH /v1/admin/projects/{project_id}` 补录。
    不传 last_active_at / milestone：图纸约定先留空（由提炼/commit 探测链路回填）。

    Args:
        name: 项目名（纯英文，同目录名），作为 project_id 与 name。
        path: 项目绝对路径。

    Returns:
        True 登记成功；False 已跳过或失败（均不抛异常）。
    """
    admin_key = os.environ.get("SGME_ADMIN_KEY")
    if not admin_key:
        print("  ⚠️ 未设置环境变量 SGME_ADMIN_KEY，跳过项目注册表登记（稍后补登）")
        return False

    payload = {"project_id": name, "name": name, "path": path}
    try:
        req = urllib.request.Request(
            SGME_PROJECTS_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": admin_key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        action = "新建" if data.get("created") else "更新"
        print(f"  ✅ 项目注册表登记完成（{action}）: {name}")
        return True
    except Exception as e:
        print(f"  ⚠️ 项目注册表登记失败（不影响立项，稍后补登）: {e}")
        return False


# ---------- 0.8 ST-15+：待办池关联（2026-08-13 接线） ----------
# 立项时把「标题含项目名的待办」标为 planned 并绑定项目，闭合"待办→项目"链路。
# 失败不阻断立项（待办可稍后人工关联）。

SGME_DEMANDS_URL = "http://localhost:9910/v1/admin/demands"


def link_demands(name: str, admin_key: str | None) -> None:
    """检索待办池中标题含项目名的条目 → 标 planned + project_id=name。

    Args:
        name: 项目名（project_id）。
        admin_key: admin key；未设置时跳过（提示手动关联）。
    """
    if not admin_key:
        print("  ⚠️ 待办池关联跳过（未设置 SGME_ADMIN_KEY，稍后人工关联）")
        return
    headers = {"X-API-Key": admin_key}
    try:
        req = urllib.request.Request(
            f"{SGME_DEMANDS_URL}?q={urllib.parse.quote(name)}&limit=50",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("items", [])
        hits = [d for d in items if d.get("status") == "pending"]
        if not hits:
            print("  ⑤ 待办池关联：无匹配待办，跳过")
            return
        linked = 0
        for d in hits:
            body = {"status": "planned", "project_id": name}
            upd = urllib.request.Request(
                f"{SGME_DEMANDS_URL}/{d['demand_id']}/status",
                data=json.dumps({"status": "planned"}).encode("utf-8"),
                method="PUT",
                headers={"Content-Type": "application/json", **headers},
            )
            with urllib.request.urlopen(upd, timeout=10) as resp2:
                pass
            patch_req = urllib.request.Request(
                f"{SGME_DEMANDS_URL}/{d['demand_id']}",
                data=json.dumps({"project_id": name}).encode("utf-8"),
                method="PATCH",
                headers={"Content-Type": "application/json", **headers},
            )
            try:
                with urllib.request.urlopen(patch_req, timeout=10):
                    pass
            except Exception:
                pass  # project_id 绑定失败不阻断
            linked += 1
        print(f"  ⑤ 待办池关联：{linked} 条待办标已立项并绑定 {name}")
    except Exception as e:
        print(f"  ⚠️ 待办池关联失败（不影响立项，稍后人工关联）: {e}")


def main():
    parser = argparse.ArgumentParser(description="项目立项一键初始化")
    parser.add_argument("name", help="项目名（纯英文，目录名）")
    parser.add_argument("--desc", default="", help="项目描述（中文）")
    parser.add_argument("--dir", default=str(PROJECTS_ROOT), help="项目根目录")
    args = parser.parse_args()

    name = args.name.strip()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        print(f"❌ 项目名非法: {name!r}（要求纯英文/数字/连字符）")
        sys.exit(1)

    root = Path(args.dir)
    proj_dir = root / name
    if proj_dir.exists():
        print(f"❌ 目录已存在: {proj_dir}")
        sys.exit(1)

    print(f"🚀 立项: {name} ({proj_dir})")
    print("=" * 50)

    # ① 创建项目文件夹
    (proj_dir / "docs" / "requirements").mkdir(parents=True)
    (proj_dir / "docs" / "design").mkdir(parents=True)
    print("  ① 项目文件夹创建 ✅")

    # ② 生成 Backlog 骨架
    backlog = BACKLOG_TEMPLATE.format(name=name)
    (proj_dir / "docs" / "requirements" / f"{name}-Backlog-v0.1.md").write_text(
        backlog, encoding="utf-8"
    )
    print(f"  ② Backlog 骨架生成 ✅ docs/requirements/{name}-Backlog-v0.1.md")

    # ③ 设计文档占位
    (proj_dir / "docs" / "design" / "README.md").write_text(
        DESIGN_README, encoding="utf-8"
    )
    print("  ③ 设计文档占位生成 ✅ docs/design/README.md")

    # ④ 登记 SGME
    register_sgme(name, str(proj_dir), args.desc)
    # ④' 登记项目注册表 project_meta（ST-16）：与上面记忆登记互补，失败不阻断立项
    register_project_meta(name, str(proj_dir))

    # ⑤ 待办池关联（2026-08-13 接线：检索标题含项目名的待办 → 标 planned + 绑定项目）
    link_demands(name, admin_key=os.environ.get("SGME_ADMIN_KEY"))

    # ⑥ git init + 首次 commit
    try:
        # 确保 git 身份（优先项目级 env，其次全局，缺则用默认）
        git_name = os.environ.get("GIT_AUTHOR_NAME", "")
        git_email = os.environ.get("GIT_AUTHOR_EMAIL", "")
        if not git_name:
            r = subprocess.run(["git", "config", "--global", "user.name"],
                               capture_output=True, text=True)
            git_name = r.stdout.strip() if r.returncode == 0 else "freehul"
        if not git_email:
            r = subprocess.run(["git", "config", "--global", "user.email"],
                               capture_output=True, text=True)
            git_email = r.stdout.strip() if r.returncode == 0 else "huliang@local"
        # 切到项目目录执行 git（subprocess cwd 在 Windows 下可能解析异常）
        old_cwd = os.getcwd()
        os.chdir(str(proj_dir))
        try:
            subprocess.run(["git", "init"], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", git_name],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", git_email],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "add", "-A"], check=True,
                           capture_output=True, text=True)
            subprocess.run(
                ["git", "commit", "-m", f"chore: 项目立项初始化（{name} Backlog v0.1 锚文档）"],
                check=True, capture_output=True, text=True,
            )
        finally:
            os.chdir(old_cwd)
        print("  ⑥ git init + 首次 commit ✅")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ git 初始化失败（手动处理）: {e.stderr[:200]}")

    print("=" * 50)
    print(f"✅ 立项完成: {proj_dir}")
    print("后续：拆 Story → 设计 → 开发（走 coding-discipline + git-pr-workflow）")


if __name__ == "__main__":
    main()
