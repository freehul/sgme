#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill_workspace.py — 技能编写工作流（SGME 2.0 设计 §5.2 操作手册）。

为什么有这个工具
================
1.x 时代技能真源是 git 裸仓：改一个技能要 clone/拉分支/commit/push，重且容易
在 IDE 里直接改库（绕开 lint 与索引）。2.0 起技能全存 ``skills.db``，本工具提供
「本地工作副本目录编辑 + 提交入库命令」：

- 拉下来就是一个普通目录（SKILL.md + references/scripts/assets），用任意编辑器改；
- ``status`` 随时看与库的差异；
- ``push`` 先跑格式门禁（lint），通过才写库，并自动记修订史；
- 不碰 git，不需要服务进程，直接读写 ``skills.db``（默认 ``SGME_HOME/data``）。

四个子命令
==========
``pull <name> --dir <目录>``
    从库拉一个技能成可编辑目录。目标目录已有未入库改动 / 非本技能时拒绝覆盖，
    ``--force`` 才整目录原子替换（会丢弃目录内全部未入库内容）。

``status --dir <目录>``
    对比本地目录与库内 sha：列出 SKILL.md 状态与附属文件的「已修改 / 新增 /
    删除」。有差异退出码 1，无差异 0（可进 CI/脚本）。目录需是 pull 出来的副本。

``push --dir <目录>``
    提交入库：前端校验 SKILL.md → 格式 lint（error 即拒绝并逐条列出）→ 写
    ``skills`` 主表 + ``skill_files`` 附属文件（多余文件删除）→ 记
    ``skill_revisions`` 修订史 → 刷新本地清单。无改动时幂等跳过。
    库内该技能在 pull 后已被别处修改时默认拒绝（先 pull 合并），``--force``
    以本地为准覆盖。

``list --dir <根目录>``
    列出根目录下全部工作副本（含根自身）及其状态，一眼看哪些有未入库改动。

典型流程
========
::

    # 1) 拉一个技能到本地工作区
    python scripts/skill_workspace.py pull pdf-tools --dir D:/work/skills/pdf-tools

    # 2) 用任意编辑器改 SKILL.md / references/...
    #    新增文件直接放进目录即可（.sgme-manifest.json 不要动）

    # 3) 看差异
    python scripts/skill_workspace.py status --dir D:/work/skills/pdf-tools
    # 技能: pdf-tools
    # SKILL.md: 已修改（本地已修改）
    # 附属文件:
    #   已修改: references/api.md
    #   新增: scripts/convert.py
    # 共 2 处差异 → 可 push

    # 4) 提交入库（lint 不过会拒绝并列出违规）
    python scripts/skill_workspace.py push --dir D:/work/skills/pdf-tools

    # 5) 批量看工作区
    python scripts/skill_workspace.py list --dir D:/work/skills

库位置
======
默认取 ``sgme.config.DATA_DIR/skills.db``（``SGME_HOME`` 环境变量可重定向）。
显式指定二选一：

::

    --data-dir D:/sgme/data     # 指向包含 skills.db 的数据目录
    --db D:/sgme/data/skills.db # 直接指向库文件

退出码：0 成功/无差异；1 业务拒绝（lint 失败、冲突、存在差异、技能不存在）；
2 用法或环境错误（目录/库不可读、JSON 清单损坏等）。所有提示为中文。

边界（迁移期须知）
==================
- 本工具写 ``skills.db``（2.0 全存库口径），**不写** 1.x 的 git 工作区
  （``skills.source_dirs``）。迁移完成前两套真源不互通：git 侧改动仍走
  ``skill_put`` / ``skills/store.py``，再由同步任务索引进库。
- 库表只承载 description/category/tags/version/pattern/uses 六类 frontmatter；
  SKILL.md 里其它自定义字段在 pull 重建时不会保留（正文与附属文件原样保留）。
- 附属文件表由 ``skill_package_dao.ensure_tables`` 保证存在（迁移期老库兜底）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# scripts/ 作为包被导入（tests）与直接执行（__main__）都要能找到仓库根
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sgme.data import skill_package_dao  # noqa: E402
from sgme.operations import skill_workspace as ws  # noqa: E402

# 各子命令共享的库定位参数（父解析器，避免每个 add_argument 重复）
_LIB_PARENT = argparse.ArgumentParser(add_help=False)
_LIB_PARENT.add_argument(
    "--data-dir",
    default=None,
    help="SGME 数据目录（内含 skills.db）；默认 sgme.config.DATA_DIR",
)
_LIB_PARENT.add_argument(
    "--db",
    default=None,
    help="直接指定 skills.db 路径（与 --data-dir 二选一，优先级更高）",
)


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _open_conn(args: argparse.Namespace) -> sqlite3.Connection:
    """按参数打开库连接；--db 指向既有库时只补建附属文件表。"""
    if args.db:
        if args.data_dir:
            raise ValueError("--db 与 --data-dir 不能同时指定")
        path = Path(args.db)
        if not path.is_file():
            raise FileNotFoundError(f"技能库不存在: {path}")
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skills'"
        ).fetchone()
        if exists is None:
            conn.close()
            raise ValueError(f"不是有效的技能库（缺少 skills 表）: {path}")
        skill_package_dao.ensure_tables(conn)
        return conn

    from sgme import config
    from sgme.data.db import connect_skills

    data_dir = Path(args.data_dir) if args.data_dir else Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return connect_skills(data_dir)


# ---------- 输出格式（全部中文） ----------


def _fmt_sha(value: str | None) -> str:
    return (value or "")[:12] or "(空)"


def _print_violations(violations: list[str]) -> None:
    for item in violations:
        _eprint(f"  - {item}")


def _cmd_pull(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    result = ws.pull_skill(conn, args.name, args.dir, force=args.force)
    if not result.get("ok"):
        _eprint(f"✗ 拉取失败: {result.get('message')}")
        return 1
    print(
        f"✓ 已拉取 {result['name']} → {result['dir']}"
        f"（{result['files']} 个文件）"
    )
    print(f"  包指纹: {_fmt_sha(result['package_sha256'])}")
    return 0


def _cmd_status(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    st = ws.status_skill(conn, args.dir)
    print(f"技能: {st['name']}")
    print(f"目录: {st['dir']}")
    print(f"库内: {'存在' if st['library_exists'] else '不存在'}")

    labels = {
        "same": "与库一致",
        "modified": "已修改",
        "missing": "缺失（本地没有 SKILL.md）",
        "added": "新增（库内尚无此技能）",
    }
    line = f"SKILL.md: {labels.get(st['skill']['state'], st['skill']['state'])}"
    if st["skill"]["state"] == "modified" and st["skill"].get("changed_by") == "library":
        line += "（库已更新，本地副本过期）"
    elif st["skill"]["state"] == "modified":
        line += "（本地已修改）"
    print(line)
    if st["skill"]["state"] in ("same", "modified"):
        print(
            f"  本地 sha256: {_fmt_sha(st['skill']['local_sha256'])}"
            f"  库内 sha256: {_fmt_sha(st['skill']['library_sha256'])}"
        )

    modified = st["files"]["modified"]
    added = st["files"]["added"]
    deleted = st["files"]["deleted"]
    diff_count = len(modified) + len(added) + len(deleted)
    if diff_count:
        print("附属文件:")
        for p in modified:
            print(f"  已修改: {p}")
        for p in added:
            print(f"  新增: {p}")
        for p in deleted:
            print(f"  删除: {p}")
    else:
        print("附属文件: 无差异")

    if st["library_changed"]:
        print("注意: 库内该技能自上次同步后已变化；push 前建议先重新 pull（或 --force）")
    if st["dirty"]:
        print(f"共 {diff_count + (0 if st['skill']['state'] == 'same' else 1)} 处差异 → 可 push")
        return 1
    print("无差异 ✓")
    return 0


def _cmd_push(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    result = ws.push_skill(conn, args.dir, force=args.force)
    if not result.get("ok"):
        code = result.get("code")
        if code == "lint_failed":
            violations = result.get("violations") or []
            _eprint(f"✗ {result.get('message')}（{len(violations)} 条违规）:")
            _print_violations(violations)
        else:
            _eprint(f"✗ 入库失败: {result.get('message')}")
            if code == "conflict":
                _eprint(
                    f"  清单包指纹: {_fmt_sha(result.get('manifest_package_sha256'))}"
                    f"  库内包指纹: {_fmt_sha(result.get('library_package_sha256'))}"
                )
        return 1

    if not result.get("changed"):
        print(f"{result['name']}: {result.get('message')}")
        return 0

    print(f"技能: {result['name']}")
    print(f"主表 sha256: {_fmt_sha(result.get('old_sha256'))} → {_fmt_sha(result.get('new_sha256'))}")
    print(
        f"附属文件: 写入 {result['attachments_written']} 个，"
        f"删除 {result['attachments_deleted']} 个"
    )
    print(f"包指纹: {_fmt_sha(result.get('package_sha256'))}")
    print(f"修订号: {result.get('revision_id')}")
    print(f"✓ 已入库 {result['name']}")
    return 0


def _cmd_list(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    entries = ws.list_workspaces(conn, args.dir)
    if not entries:
        print(f"根目录下没有技能工作副本: {args.dir}")
        return 0
    print(f"根目录: {Path(args.dir).resolve()}")
    dirty = 0
    for item in entries:
        if "error" in item:
            print(f"  {item['name']}: 读取失败（{item['error']}）")
            continue
        st = item["status"]
        if st["dirty"]:
            dirty += 1
            parts = []
            if st["skill"]["state"] != "same":
                parts.append("SKILL.md")
            counts = len(st["files"]["modified"]) + len(st["files"]["added"]) + len(
                st["files"]["deleted"]
            )
            if counts:
                parts.append(f"{counts} 个附属文件")
            note = "、".join(parts)
            suffix = "（库已更新）" if st["library_changed"] else ""
            print(f"  {item['name']}: 有未入库改动（{note}）{suffix}")
        else:
            print(f"  {item['name']}: 无变更")
    print(f"共 {len(entries)} 个工作副本，{dirty} 个有未入库改动")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill_workspace.py",
        description="技能编写工作流：本地副本编辑 + 提交入库（SGME 2.0 §5.2）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pull = sub.add_parser(
        "pull", parents=[_LIB_PARENT], help="从库拉一个技能成可编辑目录"
    )
    p_pull.add_argument("name", help="技能名（库内主键）")
    p_pull.add_argument("--dir", required=True, help="目标工作副本目录")
    p_pull.add_argument("--force", action="store_true", help="覆盖已有目录（丢弃未入库改动）")
    p_pull.set_defaults(func=_cmd_pull)

    p_status = sub.add_parser(
        "status", parents=[_LIB_PARENT], help="对比本地目录与库内 sha"
    )
    p_status.add_argument("--dir", required=True, help="工作副本目录")
    p_status.set_defaults(func=_cmd_status)

    p_push = sub.add_parser(
        "push", parents=[_LIB_PARENT], help="把本地改动提交入库（先跑格式 lint）"
    )
    p_push.add_argument("--dir", required=True, help="工作副本目录")
    p_push.add_argument("--force", action="store_true", help="库已变化时仍以本地为准覆盖")
    p_push.set_defaults(func=_cmd_push)

    p_list = sub.add_parser(
        "list", parents=[_LIB_PARENT], help="列出本地工作副本状态"
    )
    p_list.add_argument("--dir", required=True, help="工作副本根目录")
    p_list.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        conn = _open_conn(args)
    except (OSError, ValueError, sqlite3.Error) as e:
        _eprint(f"✗ 无法打开技能库: {e}")
        return 2
    try:
        return int(args.func(args, conn))
    except (OSError, ValueError, sqlite3.Error) as e:
        _eprint(f"✗ 错误: {e}")
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
