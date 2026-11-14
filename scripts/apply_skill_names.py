# -*- coding: utf-8 -*-
"""scripts/apply_skill_names.py：T5c/T10 命名+三分类导入校验落库（纯本地脚本，零 LLM）。

用途
----
把命名阶段（T5b，zcode 会话内逐片生成）产出的 ``part-XXX.meta.json`` 校验后
写回 skills.db 的 ``skill_candidates``：

    python scripts/apply_skill_names.py --dir tmp/naming [--db <path>]            # dry-run
    python scripts/apply_skill_names.py --dir tmp/naming --apply                  # 真写
    python scripts/apply_skill_names.py --dir tmp/naming --apply --overwrite     # 覆盖已命名

输入格式（T10 起：**meta.json**，name + kind + domain 一趟生成）
----
    {"part": 1, "items": [{"i": 0, "memory_id": "...", "name": "...",
                            "kind": "skill|wiki|discard", "domain": "<白名单>",
                            "why": "..."}]}
旧 ``*.names.json``（v2 纯命名格式）**忽略并提示**，不静默当合法输入。

校验规则（**不信任模型输出格式**，任一不过该条计 rejected，不写库、不中断整批）
----
| 校验项 | 规则 |
|---|---|
| 数量 | items 条数 == 输入 items 条数（否则该片整体拒收） |
| 对齐 | 每个 i 都在输入里，且 memory_id 与输入该 i 完全一致（防串号） |
| 规范 | name ``^[a-z0-9]+(-[a-z0-9]+)*$`` 且 ≤48 且不以 ``exp-`` 开头 |
| kind | ∈ {``skill``（进技能库）/ ``wiki``（进知识库）/ ``discard``（不沉淀）} |
| domain | ∈ 24 领域白名单 ∪ {``other``}（禁止 tech_stack 等记忆维度 id） |
| 重名 | 与 skills 主表比对 + 本次导入内去重：撞名自动加 ``-2``/``-3`` |
| 同指纹 | 同 content_sha256 本次只收一条（一份正文一个名字，其余靠广播） |

写入语义
----
- 写 ``suggested_name`` + ``suggested_kind`` + ``suggested_domain`` +
  ``naming_model='zcode-glm'`` + ``named_at``（五字段一并原子落库）；
- **按 content_sha256 广播**：同指纹的其他候选（同一正文）共用名字与分类
  （dup_shares 计数）；已有 suggested_name 的行默认跳过，``--overwrite`` 才覆盖；
- 默认 dry-run（零写入），``--apply`` 才真写；老库缺列时 apply 前先跑幂等迁移。

汇总：``{parts, items, applied, rejected, conflicts, dup_shares, skipped_named,
by_kind, by_domain, ignored_names_files, rejected_details}``。

退出码：0 = 全部干净；1 = 有 rejected（rejected 是信号，报给 lead 一起看，
不要改数据迁就脚本）；2 = 用法/路径错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# 允许直接 `python scripts/xxx.py` 时从仓库根导入 sgme
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgme.data import skill_candidates_dao, skills_dao  # noqa: E402

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX_LEN = 48
#: 命名来源模型（T5b 的命名本体是 zcode 会话内置 GLM）
NAME_MODEL = "zcode-glm"
MAX_REJECTED_DETAILS = 20

#: T10 三分类（三个去向，不是三档质量）：判据见任务清单 §1.2
VALID_KINDS: tuple[str, ...] = ("skill", "wiki", "discard")

#: T10 领域白名单（24 领域 ∪ other；来源：真技能主表现存领域词。
#: 禁止把 tech_stack / environment 这类记忆维度 id 当 domain 写入）
DOMAIN_WHITELIST: frozenset[str] = frozenset({
    "software-development", "hermes", "ai", "creative", "methodology", "github",
    "design", "research", "network", "social-media", "devops", "tool",
    "verification", "data", "security", "adapter", "media", "windows",
    "email", "productivity", "linux", "business", "video-production",
    "orchestration", "other",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_valid_name(name: str) -> bool:
    """命名规范：小写字母/数字/连字符、≤48、不以 exp- 开头（占位名前缀）。"""
    return (
        bool(NAME_RE.match(name))
        and len(name) <= NAME_MAX_LEN
        and not name.startswith("exp-")
    )


def _dedupe_name(name: str, used: set[str]) -> str:
    """撞名自动加 -2/-3…；加后缀会超长时先截短基名（不留尾连字符）。"""
    if name not in used:
        return name
    n = 2
    while True:
        suffix = f"-{n}"
        base = name[: NAME_MAX_LEN - len(suffix)].rstrip("-")
        candidate = f"{base}{suffix}"
        if candidate not in used and len(candidate) <= NAME_MAX_LEN:
            return candidate
        n += 1


def _find_pairs(
    naming_dir: Path,
) -> tuple[list[tuple[Path, Path]], list[Path], list[Path]]:
    """meta 文件与输入分片配对；无配对输入的 meta 记孤儿；旧 names.json 记忽略。"""
    pairs: list[tuple[Path, Path]] = []
    orphans: list[Path] = []
    for meta_path in sorted(naming_dir.glob("part-*.meta.json")):
        # part-001.meta.json -> part-001.json
        part_path = naming_dir / f"{meta_path.name[:-len('.meta.json')]}.json"
        if part_path.exists():
            pairs.append((part_path, meta_path))
        else:
            orphans.append(meta_path)
    legacy: list[Path] = sorted(naming_dir.glob("part-*.names.json"))
    return pairs, orphans, legacy


def run(
    *,
    naming_dir: Path | str,
    db_path: Path | str,
    apply: bool,
    overwrite: bool,
) -> dict:
    """扫 names 文件 → 逐条校验 → （apply 时）落库。

    Returns:
        ``{parts, items, applied, rejected, conflicts, dup_shares, skipped_named,
        would_apply, rejected_details}``
    """
    summary: dict = {
        "apply": bool(apply),
        "overwrite": bool(overwrite),
        "parts": 0,
        "items": 0,
        "applied": 0,
        "would_apply": 0,
        "rejected": 0,
        "conflicts": 0,
        "dup_shares": 0,
        "skipped_named": 0,
        "by_kind": {},
        "by_domain": {},
        "ignored_names_files": [],
        "rejected_details": [],
    }

    def reject(detail: str) -> None:
        summary["rejected"] += 1
        summary["rejected_details"].append(detail)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if apply:
            # 老库补列（幂等；dry-run 模式零写入不做迁移）：
            # T5c 命名三列 + T9 三分类两列——本脚本要写的列必须先就位
            from sgme.data.db import (
                _migrate_skill_candidates_naming,
                _migrate_skill_candidates_triage_columns,
            )

            _migrate_skill_candidates_naming(conn)
            _migrate_skill_candidates_triage_columns(conn)

        used_names: set[str] = skills_dao.all_skill_names(conn)
        named_at = _now_iso()
        pairs, orphans, legacy = _find_pairs(Path(naming_dir))
        # T10：旧 *.names.json 是 v2 遗留格式——显式忽略并提示，不当合法输入
        for names_path in legacy:
            summary["ignored_names_files"].append(names_path.name)
        for meta_path in orphans:
            reject(f"{meta_path.name}: 无配对的输入分片文件，整份跳过")

        for part_path, meta_path in pairs:
            try:
                items = (json.loads(part_path.read_text(encoding="utf-8"))
                         .get("items") or [])
                metas = (json.loads(meta_path.read_text(encoding="utf-8"))
                         .get("items") or [])
            except Exception as e:  # noqa: BLE001——坏文件只拒这一片
                reject(f"{meta_path.name}: 读取/解析失败（{e}）")
                continue

            summary["parts"] += 1
            summary["items"] += len(items)
            if len(metas) != len(items):
                # 数量不符 → 该片整体拒收（逐条对齐已不可信）；明细行不再重复计数
                for _it in items:
                    summary["rejected"] += 1
                summary["rejected_details"].append(
                    f"{meta_path.name}: 数量不符（meta {len(metas)} != items"
                    f" {len(items)}），整片拒收"
                )
                continue

            by_index = {
                int(it["i"]): it for it in items
                if isinstance(it, dict) and str(it.get("i", "")).lstrip("-").isdigit()
            }
            claimed_shas: dict[str, str] = {}  # sha -> 本次已收的 memory_id

            for entry in metas:
                if not isinstance(entry, dict):
                    reject(f"{meta_path.name}: 非法条目（非对象）")
                    continue
                idx = entry.get("i")
                mid = str(entry.get("memory_id") or "")
                name = str(entry.get("name") or "").strip()
                kind = str(entry.get("kind") or "").strip()
                domain = str(entry.get("domain") or "").strip()
                input_item = by_index.get(idx) if isinstance(idx, int) else None
                if input_item is None:
                    reject(f"{meta_path.name}: i={idx!r} 不在输入分片里")
                    continue
                input_mid = str(input_item.get("memory_id") or "")
                if mid != input_mid:
                    # 串号：meta 的 memory_id 与输入该 i 的不一致
                    reject(
                        f"{meta_path.name}: i={idx} memory_id 串号"
                        f"（meta={mid or '空'} != 输入={input_mid}）"
                    )
                    continue
                if not _is_valid_name(name):
                    reject(
                        f"{meta_path.name}: i={idx} 名字非法: {name!r}"
                        f"（规则：{NAME_RE.pattern}，≤{NAME_MAX_LEN}，不以 exp- 开头）"
                    )
                    continue
                if kind not in VALID_KINDS:
                    reject(
                        f"{meta_path.name}: i={idx} kind 非法: {kind!r}"
                        f"（合法值: {', '.join(VALID_KINDS)}）"
                    )
                    continue
                if domain not in DOMAIN_WHITELIST:
                    reject(
                        f"{meta_path.name}: i={idx} domain 非法: {domain!r}"
                        f"（须在 24 领域白名单 ∪ other，禁止记忆维度 id）"
                    )
                    continue
                row = skill_candidates_dao.get_candidate(conn, input_mid)
                if row is None:
                    reject(f"{meta_path.name}: i={idx} 候选不存在: {input_mid}")
                    continue
                sha = str(row.get("content_sha256") or "")
                if sha and sha in claimed_shas:
                    reject(
                        f"{meta_path.name}: i={idx} 同指纹已在本次导入收过"
                        f"（{claimed_shas[sha]}），一份正文一个名字"
                    )
                    continue

                # T10：两维分布按通过校验的条目计（dry-run/apply 同口径）
                summary["by_kind"][kind] = summary["by_kind"].get(kind, 0) + 1
                summary["by_domain"][domain] = summary["by_domain"].get(domain, 0) + 1

                final = _dedupe_name(name, used_names)
                if final != name:
                    summary["conflicts"] += 1
                used_names.add(final)
                if sha:
                    claimed_shas[sha] = input_mid

                # 广播目标：主条目 + 同指纹的其他候选行
                targets = [input_mid]
                if sha:
                    targets += [
                        m for m in skill_candidates_dao.memory_ids_by_content_sha(conn, sha)
                        if m != input_mid
                    ]
                main_row = row
                if main_row.get("suggested_name") and not overwrite:
                    summary["skipped_named"] += 1
                    continue
                summary["would_apply"] += 1
                if apply:
                    skill_candidates_dao.set_candidate_naming(
                        conn, input_mid, final, NAME_MODEL, named_at,
                        kind=kind, domain=domain,
                    )
                    summary["applied"] += 1
                for target_mid in targets[1:]:
                    trow = skill_candidates_dao.get_candidate(conn, target_mid)
                    if trow is None:
                        continue
                    if trow.get("suggested_name") and not overwrite:
                        continue
                    summary["dup_shares"] += 1
                    if apply:
                        skill_candidates_dao.set_candidate_naming(
                            conn, target_mid, final, NAME_MODEL, named_at,
                            kind=kind, domain=domain,
                        )
        if apply:
            conn.commit()
    finally:
        conn.close()
    return summary


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导入技能命名（T5c，校验后写回候选池）")
    parser.add_argument("--dir", required=True, help="分片目录（含 part-XXX.json 与 *.names.json）")
    parser.add_argument("--db", default=None,
                        help="skills.db 路径（缺省取 sgme 配置的 DATA_DIR/skills.db）")
    parser.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖已有 suggested_name（默认跳过）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.db:
        db_path = Path(args.db)
    else:
        from sgme import config as sgme_config

        db_path = Path(sgme_config.DATA_DIR) / "skills.db"
    if not db_path.exists():
        print(f"skills.db 不存在: {db_path}（用 --db 显式指定）", file=sys.stderr)
        return 2
    naming_dir = Path(args.dir)
    if not naming_dir.is_dir():
        print(f"分片目录不存在: {naming_dir}", file=sys.stderr)
        return 2

    summary = run(naming_dir=naming_dir, db_path=db_path,
                  apply=args.apply, overwrite=args.overwrite)
    head = {k: v for k, v in summary.items() if k != "rejected_details"}
    print(json.dumps(head, ensure_ascii=False))
    for detail in summary["rejected_details"][:MAX_REJECTED_DETAILS]:
        print(f"  rejected: {detail}")
    if len(summary["rejected_details"]) > MAX_REJECTED_DETAILS:
        print(f"  ...（其余 {len(summary['rejected_details']) - MAX_REJECTED_DETAILS} 条略）")
    return 0 if summary["rejected"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
