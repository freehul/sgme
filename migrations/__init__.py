"""migrations/：v0.7 一次性运维迁移脚本包（**位于项目根，不进 `sgme/` 包**）。

边界铁律（§2.1 / §2.1.1）：
- 迁移脚本是一次性运维资产，放在 `sgme/` 之外的项目根，
  阶段 2 的 `git mv sgme/storage sgme/data` 对它零影响（只需改一行 import）。
- 它通过 `sgme.storage.db` 拿连接，**不建表、不补列**（表结构归 db.py 的 `*_DDL`），
  **不写 FTS 代码**（FTS 归 search 层 `init_scenes_fts`），只搬存量数据 + 备份 + 清理旧物。

用法：

    python -m migrations                 # 对默认 data 目录跑全部未应用迁移
    python -m migrations --data-dir X    # 指定数据目录（推荐先在真实库的副本上演练）
    python -m migrations --dry-run       # 只打印将要执行的迁移，不落任何改动
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sgme.migrations")

__all__ = ["run_all", "pending"]


def _open_conns(data_dir: Path) -> dict:
    """打开三库连接（db.py 的 `connect_*` 会顺带把新表结构建好）。"""
    from sgme.data import db as db_mod

    mem, session, wiki = db_mod.init_databases(data_dir)
    return {"data_dir": data_dir, "memory": mem, "session": session, "wiki": wiki}


def _close_conns(conns: dict) -> None:
    """关闭三库连接（忽略关闭期异常）。"""
    from sgme.data import db as db_mod

    for key in ("memory", "session", "wiki"):
        conn = conns.get(key)
        if conn is None:
            continue
        try:
            db_mod.close(conn)
        except Exception:
            pass


def pending(data_dir: str | Path | None = None) -> list[str]:
    """返回尚未应用的迁移名列表（只读探测，不做任何写入）。"""
    from sgme import config
    from migrations import _registry

    d = Path(data_dir) if data_dir else config.DATA_DIR
    conns = _open_conns(d)
    try:
        names: list[str] = []
        for mig in _registry.load_migrations():
            applied = all(
                _registry.is_applied(conns[t], mig.version) for t in mig.targets
            )
            if not applied:
                names.append(mig.name)
        return names
    finally:
        _close_conns(conns)


def run_all(
    data_dir: str | Path | None = None,
    cfg: dict | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """按序执行全部未应用迁移。

    流程（§2.1）：先 `connect_*` 由 db.py 建好三库新表 → 再执行未登记版本的搬运 →
    最后写 `schema_versions` 登记。

    Args:
        data_dir: 数据目录；缺省取 `config.DATA_DIR`。
        cfg: 预留的配置字典（当前迁移不依赖配置，保留形参以稳定对外签名）。
        force: True 时忽略 applied 标记强制重跑（迁移本身可重入）。
        dry_run: True 时只返回待执行清单，不做任何改动。

    Returns:
        `{"data_dir", "applied": [...], "skipped": [...], "results": {name: 摘要}}`。
    """
    from sgme import config
    from migrations import _registry

    d = Path(data_dir) if data_dir else config.DATA_DIR
    _ = cfg  # 当前迁移不读配置；保留形参以稳定对外签名

    conns = _open_conns(d)
    applied: list[str] = []
    skipped: list[str] = []
    results: dict[str, Any] = {}
    try:
        for mig in _registry.load_migrations():
            already = all(
                _registry.is_applied(conns[t], mig.version) for t in mig.targets
            )
            if already and not force:
                logger.info("迁移 %s 已应用，跳过", mig.name)
                skipped.append(mig.name)
                continue
            if dry_run:
                logger.info("[dry-run] 将执行迁移 %s", mig.name)
                applied.append(mig.name)
                continue

            logger.info("执行迁移 %s ...", mig.name)
            results[mig.name] = mig.up(conns)
            for target in mig.targets:
                _registry.mark_applied(conns[target], mig.version, mig.name)
            applied.append(mig.name)
            logger.info("迁移 %s 完成", mig.name)
    finally:
        _close_conns(conns)

    return {
        "data_dir": str(d),
        "applied": applied,
        "skipped": skipped,
        "results": results,
    }
