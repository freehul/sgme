#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_2_0_baseline.py：SGME 2.0 迁移阶段 0（冻结与备份）的**只读**采集器。

依据 `docs/design/SGME2.0-设计方案-V1.0.md» §9.1 / §9.4。阶段 0 的职责是「先冻结事实，
再动手」：没有基线，后面所有校验（行数守恒、文本完整性、向量/索引一致、scene_key
unknown 占比、知识库修订史）都没有比对对象。

采集面（与 §9.1 表逐项对应）：
- 记忆：memories 行数 + status 分布 / superseded 占比
- 归档：memory_archive 行数
- 向量：memory_vectors 行数 + 未嵌清单条数（LEFT JOIN memories）
- 全文索引：memories_fts 行数（完整性检查需写权限，留阶段 4）
- 会话原件：raw_files 行数 / 状态分布 / started_at 范围 / raw 目录体积
- 知识库：wiki_pages 行数 / status 分布 / wiki_links / 2.0 九列与两表
- 技能：skills / skill_files / skill_uses / skill_revisions / skill_meta
- 凭据：credentials / credential_audit + 2.0 四列
- 角色：persona_* 各表行数（agent 配置在 agent_keys.json，缺失进 warnings）
- 场景（冻结前基线）：scenes / scene_memories / scene_vectors / scene_versions
- 迁移关注：TTL 分桶 / agent_tag 覆盖率 / 2.0 列存在性 / lifecycle 分布 / scene_key 非空率

另存内容哈希清单（§9.1 三件套之二）：memories 抽样 500、wiki 20、skills 20
（memory_id → sha256(content)），供 §9.4 第 2/6/7 项在迁移后逐条比对。

纪律：
- 全程 mode=ro：不建表、不写库、不跑 FTS rebuild；缺库/缺表记 null 或进 warnings，不失败。
- 报告同时落 JSON（机器比对）与 Markdown（人读）；路径默认脱敏为目录名。
- 只在阶段 0 与每次「重取基线」时跑：这是快照，不是监控。

用法：
    python scripts/migrate_2_0_baseline.py --data-dir data --out-dir docs/migration
    docker exec -i sgme python scripts/migrate_2_0_baseline.py \
        --data-dir /data/data --out-dir /tmp/migration
    python scripts/migrate_2_0_baseline.py --sample-memories 2000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# 允许从项目根直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 五库（skills / credentials 属扩展模块，缺失不算失败）
DB_FILES = ("memory.db", "session.db", "wiki.db", "skills.db", "credentials.db")

# 2.0 迁移阶段 1 关注的列（memories / memory_archive 同名）
TWO_0_MEMORY_COLUMNS = (
    "derivation",
    "modality",
    "lifecycle",
    "scene_key",
    "ttl_renew_count",
    "ttl_renewed_at",
    "inject_tier",
    "meaning_anchor",
    "needs_review",
)

# 2.0 迁移阶段 1 关注的正式表
TWO_0_TABLES = (
    "ttl_disposition",
    "deletion_audit",
    "rule_sync_ledger",
    "rule_sync_daily",
    "llm_usage_daily",
)

SCENE_TABLES = ("scenes", "scene_memories", "scene_vectors", "scene_versions")


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（项目统一口径）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ro_connect(path: Path) -> sqlite3.Connection | None:
    """只读打开（mode=ro）；文件不存在返回 None。

    WAL 库在只读模式下依赖已存在的 -wal/-shm；打开失败时降级为 immutable=1
    （旧快照副本常见），仍不写库。
    """
    if not path.is_file():
        return None
    base = path.as_posix()
    for uri in (f"file:{base}?mode=ro", f"file:{base}?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.Error:
            continue
    return None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int | None:
    """表行数；表不存在返回 None（0 是真实空表，两者不可混淆）。"""
    if table not in _tables(conn):
        return None
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql, params).fetchone()[0])


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if table not in _tables(conn):
        return []
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _group_counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int | None]:
    """按列分组计数；column 不存在返回 {}。NULL 记为 "<null>"。"""
    if column not in _columns(conn, table):
        return {}
    rows = conn.execute(
        f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} ORDER BY COUNT(*) DESC"
    ).fetchall()
    out: dict[str, int | None] = {}
    for k, v in rows:
        out["<null>" if k is None else str(k)] = int(v)
    return out


def _schema_versions(conn: sqlite3.Connection) -> list[dict]:
    """schema_versions 登记行（每库自持一份；缺表返回 []）。"""
    if "schema_versions" not in _tables(conn):
        return []
    return [
        {"version": r[0], "name": r[1], "applied_at": r[2]}
        for r in conn.execute(
            "SELECT version, name, applied_at FROM schema_versions ORDER BY version"
        ).fetchall()
    ]


def _sha256_text(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _hash_sample(
    conn: sqlite3.Connection, table: str, id_col: str, content_col: str, limit: int
) -> list[dict]:
    """确定性抽样（按 id 排序取前 N）→ [{"id":..., "sha256":...}]。"""
    if table not in _tables(conn) or content_col not in _columns(conn, table):
        return []
    if id_col not in _columns(conn, table):
        return []
    rows = conn.execute(
        f"SELECT {id_col}, {content_col} FROM {table} WHERE {id_col} IS NOT NULL "
        f"ORDER BY {id_col} LIMIT ?",
        (max(0, limit),),
    ).fetchall()
    return [{"id": str(r[0]), "sha256": _sha256_text(r[1])} for r in rows]


def _dir_size(path: Path) -> dict | None:
    """目录体积汇总（文件数 + 字节数）；不存在返回 None。"""
    if not path.is_dir():
        return None
    files = 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            files += 1
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return {"files": files, "bytes": total}


def _file_digest(path: Path) -> dict | None:
    """单文件 sha256 + 体积（分块读）。"""
    if not path.is_file():
        return None
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": h.hexdigest()}


def collect_memory(conn: sqlite3.Connection) -> dict:
    """记忆库基线（§9.1 记忆/归档/向量/索引 + 迁移关注项）。"""
    tables = _tables(conn)
    total = _count(conn, "memories")
    vectors = _count(conn, "memory_vectors")
    missing_vectors = None
    if "memory_vectors" in tables and "memories" in tables:
        missing_vectors = int(
            conn.execute(
                "SELECT COUNT(*) FROM memories m "
                "LEFT JOIN memory_vectors v ON v.memory_id = m.memory_id "
                "WHERE v.memory_id IS NULL"
            ).fetchone()[0]
        )

    ttl_buckets: dict[str, int | None] = {}
    if "ttl_days" in _columns(conn, "memories"):
        ttl_buckets = {
            "90": _count(conn, "memories", "ttl_days = 90"),
            "30": _count(conn, "memories", "ttl_days = 30"),
            "7": _count(conn, "memories", "ttl_days = 7"),
            "null": _count(conn, "memories", "ttl_days IS NULL"),
        }
        other = [
            int(r[0])
            for r in conn.execute(
                "SELECT COUNT(*) FROM memories WHERE ttl_days IS NOT NULL "
                "AND ttl_days NOT IN (90,30,7) GROUP BY ttl_days"
            ).fetchall()
        ]
        ttl_buckets["other"] = sum(other)

    agent_tag = {
        "total": total,
        "tagged": _count(conn, "memories", "agent_tag IS NOT NULL AND agent_tag != ''"),
    }
    if agent_tag["total"]:
        agent_tag["ratio"] = round(agent_tag["tagged"] / agent_tag["total"], 4)

    two_0_cols = {col: (col in _columns(conn, "memories")) for col in TWO_0_MEMORY_COLUMNS}
    archive_cols = {col: (col in _columns(conn, "memory_archive")) for col in TWO_0_MEMORY_COLUMNS}

    dims = []
    if "dimension_registry" in tables:
        dim_cols = _columns(conn, "dimension_registry")
        has_prio = "priority" in dim_cols
        sql = (
            "SELECT id, display_name, category, active, "
            + ("priority" if has_prio else "NULL")
            + " FROM dimension_registry ORDER BY "
            + ("priority IS NULL, priority" if has_prio else "id")
        )
        dims = [
            {"id": r[0], "display_name": r[1], "category": r[2], "active": r[3], "priority": r[4]}
            for r in conn.execute(sql).fetchall()
        ]

    return {
        "schema_versions": [
            {"version": r[0], "name": r[1], "applied_at": r[2]}
            for r in conn.execute(
                "SELECT version, name, applied_at FROM schema_versions ORDER BY version"
            ).fetchall()
        ]
        if "schema_versions" in tables
        else [],
        "memories": {
            "total": total,
            "by_status": _group_counts(conn, "memories", "status"),
            "non_active": _count(conn, "memories", "status != 'active'"),
            "superseded_rejected": _count(
                conn, "memories", "status = 'rejected' AND reject_reason LIKE '%替代%'"
            ),
        },
        "memory_archive": _count(conn, "memory_archive"),
        "memory_vectors": {"rows": vectors, "missing": missing_vectors},
        "memory_stats": _count(conn, "memory_stats"),
        "memory_tags": _count(conn, "memory_tags"),
        "memory_edges": _count(conn, "memory_edges"),
        "memory_usage_daily": _count(conn, "memory_usage_daily"),
        "memories_fts_rows": _count(conn, "memories_fts"),
        "valid_period": {
            "valid_from_non_null": _count(conn, "memories", "valid_from IS NOT NULL"),
            "valid_to_non_null": _count(conn, "memories", "valid_to IS NOT NULL"),
        },
        "ttl_buckets": ttl_buckets,
        "agent_tag_coverage": agent_tag,
        "dimension_registry": dims,
        "scene_tables": {name: _count(conn, name) for name in SCENE_TABLES},
        "two_0": {
            "columns_present": sum(1 for v in two_0_cols.values() if v),
            "columns_total": len(two_0_cols),
            "memories_columns": two_0_cols,
            "archive_columns": archive_cols,
            "tables": {name: (_count(conn, name) is not None) for name in TWO_0_TABLES},
            "scene_key_non_null": _count(
                conn, "memories", "scene_key IS NOT NULL AND scene_key != ''"
            )
            if "scene_key" in _columns(conn, "memories")
            else None,
            "lifecycle_distribution": _group_counts(conn, "memories", "lifecycle"),
            "inject_tier_distribution": _group_counts(conn, "memories", "inject_tier"),
        },
        "persona_tables": {
            name: _count(conn, name)
            for name in ("persona_traits", "persona_reports", "persona_state")
        },
    }


def collect_session(conn: sqlite3.Connection, data_dir: Path, redact: bool) -> dict:
    """会话原件基线（§9.1 会话原件行）。"""
    # raw 目录解析（2026-11-04 生产实测）：容器里 SGME_HOME=/data、data_dir=/data/data、
    # 原件在 /data/raw —— 即 data_dir 的**兄弟目录**，不是子目录。两处都试，先命中先用。
    raw_dir = next(
        (p for p in (data_dir / "raw", data_dir.parent / "raw") if p.is_dir()),
        data_dir / "raw",
    )
    rng = None
    raw_cols = _columns(conn, "raw_files")
    if raw_cols:
        # 生产实测（2026-11-04）：有 1 行 started_at 为空串，MIN() 会被它拉成 ''，
        # 必须同时排除 NULL 与空串，否则时间范围恒判"无数据"。
        row = conn.execute(
            "SELECT MIN(started_at), MAX(started_at) FROM raw_files "
            "WHERE started_at IS NOT NULL AND started_at != ''"
        ).fetchone()
        if row and row[0]:
            rng = {"min_started_at": row[0], "max_started_at": row[1]}
    return {
        "raw_files": _count(conn, "raw_files"),
        "raw_files_by_status": _group_counts(conn, "raw_files", "status"),
        "raw_files_time_range": rng,
        "raw_files_by_agent": _group_counts(conn, "raw_files", "agent_id"),
        "refine_cursor": _count(conn, "refine_cursor"),
        "schema_versions": _schema_versions(conn),
        "raw_dir": _dir_size(raw_dir),
        "raw_dir_name": raw_dir.name if redact else raw_dir.as_posix(),
    }


def collect_wiki(conn: sqlite3.Connection) -> dict:
    """知识库基线（§9.1 知识库行 + 2.0 九列/两表）。"""
    cols = _columns(conn, "wiki_pages")
    nine = (
        "page_type",
        "headline",
        "content_hash",
        "heat_score",
        "last_recalled_at",
        "view_count",
        "dirty",
        "updated_by",
        "my_notes",
    )
    return {
        "wiki_pages": _count(conn, "wiki_pages"),
        "wiki_pages_by_status": _group_counts(conn, "wiki_pages", "status"),
        "wiki_pages_by_category": _group_counts(conn, "wiki_pages", "category"),
        "wiki_links": _count(conn, "wiki_links"),
        "wiki_revisions": _count(conn, "wiki_revisions"),
        "wiki_categories": _count(conn, "wiki_categories"),
        "schema_versions": _schema_versions(conn),
        "two_0": {
            "columns_present": sum(1 for c in nine if c in cols),
            "columns_total": len(nine),
            "columns": {c: (c in cols) for c in nine},
        },
    }


def collect_skills(conn: sqlite3.Connection) -> dict:
    """技能库基线（§9.1 技能行）。"""
    files_bytes = None
    if "skill_files" in _tables(conn):
        cols = _columns(conn, "skill_files")
        if "size" in cols:
            files_bytes = int(
                conn.execute("SELECT COALESCE(SUM(size),0) FROM skill_files").fetchone()[0]
            )
        elif "content" in cols:
            files_bytes = int(
                conn.execute("SELECT COALESCE(SUM(LENGTH(content)),0) FROM skill_files").fetchone()[0]
            )
    return {
        "skills": _count(conn, "skills"),
        "skills_by_status": _group_counts(conn, "skills", "status"),
        "skill_files": _count(conn, "skill_files"),
        "skill_files_bytes": files_bytes,
        "skill_uses": _count(conn, "skill_uses"),
        "skill_revisions": _count(conn, "skill_revisions"),
        "skill_meta": _count(conn, "skill_meta"),
        "schema_versions": _schema_versions(conn),
    }


def collect_credentials(conn: sqlite3.Connection) -> dict:
    """凭据库基线（§9.1 角色/agent 行的凭据侧；2.0 四列存在性）。"""
    cols = _columns(conn, "credentials")
    four = ("scope", "cred_type", "expires_at", "last_used_at")
    return {
        "credentials": _count(conn, "credentials"),
        "credentials_by_status": _group_counts(conn, "credentials", "status"),
        "credential_audit": _count(conn, "credential_audit"),
        "schema_versions": _schema_versions(conn),
        "two_0": {
            "columns_present": sum(1 for c in four if c in cols),
            "columns_total": len(four),
            "columns": {c: (c in cols) for c in four},
        },
    }


def _md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def render_markdown(report: dict) -> str:
    """人读版（§9.1 表 + 迁移关注项 + 哈希清单摘要）。"""
    m = report["databases"].get("memory") or {}
    s = report["databases"].get("session") or {}
    w = report["databases"].get("wiki") or {}
    k = report["databases"].get("skills") or {}
    c = report["databases"].get("credentials") or {}
    lines: list[str] = []
    lines.append(f"# SGME 2.0 迁移基线（阶段 0 · {report['label']}）")
    lines.append("")
    lines.append(
        f"> 生成时间：{report['generated_at']} ｜ 数据目录：{report['data_dir']} ｜ "
        "采集器：scripts/migrate_2_0_baseline.py（只读）"
    )
    lines.append(
        "> 用途：§9.4 十项校验的比对对象（行数守恒 / 文本完整性 / 向量一致 / 索引一致 / "
        "知识库修订史 / scene_key 基线）。"
    )
    lines.append("")
    if report["warnings"]:
        lines.append("**警告**：")
        for wmsg in report["warnings"]:
            lines.append(f"- {wmsg}")
        lines.append("")

    lines.append("## 一、§9.1 基线表")
    lines.append("")
    mem = m.get("memories", {})
    two = m.get("two_0", {})
    lines.append(
        _md_table(
            ["对象", "基线值", "备注"],
            [
                ["memories", mem.get("total"), "status 分布见第二节"],
                ["memory_archive", m.get("memory_archive"), ""],
                ["memory_vectors", (m.get("memory_vectors") or {}).get("rows"),
                 "未嵌 " + str((m.get("memory_vectors") or {}).get("missing"))],
                ["memories_fts", m.get("memories_fts_rows"), "完整性检查留阶段 4（需写权限）"],
                ["memory_tags / memory_edges", f"{m.get('memory_tags')} / {m.get('memory_edges')}", ""],
                ["memory_stats / memory_usage_daily",
                 f"{m.get('memory_stats')} / {m.get('memory_usage_daily')}", ""],
                ["raw_files", s.get("raw_files"), str(s.get("raw_files_by_status"))],
                ["raw 目录", s.get("raw_dir"), str(s.get("raw_dir_name"))],
                ["wiki_pages / wiki_links", f"{w.get('wiki_pages')} / {w.get('wiki_links')}", ""],
                ["skills / skill_files", f"{k.get('skills')} / {k.get('skill_files')}",
                 "附件 " + str(k.get("skill_files_bytes")) + " B"],
                ["credentials / audit", f"{c.get('credentials')} / {c.get('credential_audit')}", ""],
                ["scenes 四表",
                 " / ".join(str((m.get("scene_tables") or {}).get(t)) for t in SCENE_TABLES),
                 "冻结前基线（9.6 下线）"],
                ["persona 三表",
                 " / ".join(str(v) for v in (m.get("persona_tables") or {}).values()),
                 "traits / reports / state"],
            ],
        )
    )
    lines.append("")
    lines.append("## 二、memories 状态分布")
    lines.append("")
    lines.append(
        _md_table(["status", "条数"], [[kk, vv] for kk, vv in (mem.get("by_status") or {}).items()])
    )
    lines.append("")
    lines.append("## 三、迁移关注项")
    lines.append("")
    lines.append(
        _md_table(
            ["项", "值", "对迁移的意义"],
            [
                ["2.0 memories 列", f"{two.get('columns_present')}/{two.get('columns_total')}",
                 "阶段 1 加列进度"],
                ["2.0 正式表",
                 ", ".join(f"{n}={'✓' if v else '✗'}" for n, v in (two.get("tables") or {}).items()),
                 "阶段 1 建表进度"],
                ["scene_key 非空", two.get("scene_key_non_null"), "阶段 3 回填后应 ≥99%（9.4 #8）"],
                ["lifecycle 分布", two.get("lifecycle_distribution"), "阶段 1 回填后才非空"],
                ["TTL 分桶", m.get("ttl_buckets"), "90 天占比高 → 2027-01 集中到期（D-52 压力点）"],
                ["agent_tag 覆盖", m.get("agent_tag_coverage"), "scene_key 硬门基线（3.0.2）"],
                ["valid_to 非空", (m.get("valid_period") or {}).get("valid_to_non_null"),
                 "闸门 A 收窄到 planned/wished"],
                ["知识库 2.0 列",
                 f"{(w.get('two_0') or {}).get('columns_present')}/{(w.get('two_0') or {}).get('columns_total')}",
                 "§5.1 九列"],
                ["知识库修订史/分类表", f"{w.get('wiki_revisions')} / {w.get('wiki_categories')}",
                 "阶段 3 每页首条快照"],
                ["凭据 2.0 列",
                 f"{(c.get('two_0') or {}).get('columns_present')}/{(c.get('two_0') or {}).get('columns_total')}",
                 "D-37 扩展位"],
            ],
        )
    )
    lines.append("")
    lines.append("## 四、维度注册表")
    lines.append("")
    lines.append(
        _md_table(
            ["id", "display_name", "category", "active", "priority"],
            [
                [d["id"], d["display_name"], d["category"], d["active"], d["priority"]]
                for d in (m.get("dimension_registry") or [])
            ],
        )
    )
    lines.append("")
    lines.append("## 五、库文件指纹")
    lines.append("")
    lines.append(
        _md_table(
            ["文件", "字节", "sha256"],
            [[name, info["bytes"], info["sha256"]] for name, info in report["files"].items() if info],
        )
    )
    lines.append("")
    lines.append("## 六、内容哈希清单（抽样）")
    lines.append("")
    lines.append(
        _md_table(["对象", "抽样条数"], [[name, len(items)] for name, items in report["hashes"].items()])
    )
    lines.append("")
    lines.append(
        "> 完整哈希清单位于同名 .json 的 hashes 字段；迁移后按同一条目逐条比对（§9.4 #2/#6/#7）。"
    )
    return "\n".join(lines) + "\n"


def collect_report(
    data_dir: str | Path,
    *,
    sample_memories: int = 500,
    sample_wiki: int = 20,
    sample_skills: int = 20,
    label: str | None = None,
    redact_paths: bool = True,
) -> dict:
    """只读采集并返回 report 字典（**不落盘**；供阶段 0 报告与迁移校验复用）。"""
    data = Path(data_dir)
    if not (data / "memory.db").is_file():
        raise FileNotFoundError(f"memory.db 不存在：{data}")
    label = label or datetime.now(UTC).strftime("%Y%m%d")
    warnings: list[str] = []
    report: dict = {
        "kind": "sgme-2.0-migration-baseline",
        "label": label,
        "generated_at": _now_iso(),
        "data_dir": data.name if redact_paths else data.as_posix(),
        "samples": {"memories": sample_memories, "wiki": sample_wiki, "skills": sample_skills},
        "databases": {},
        "files": {},
        "hashes": {},
        "warnings": warnings,
    }

    conns: dict[str, sqlite3.Connection] = {}
    for db_file in DB_FILES:
        conn = _ro_connect(data / db_file)
        if conn is None:
            warnings.append(f"{db_file} 缺失或无法只读打开 → 该库基线记为 null")
            continue
        conns[db_file] = conn

    try:
        mem = conns.get("memory.db")
        report["databases"]["memory"] = collect_memory(mem) if mem else None
        ses = conns.get("session.db")
        report["databases"]["session"] = collect_session(ses, data, redact_paths) if ses else None
        wiki = conns.get("wiki.db")
        report["databases"]["wiki"] = collect_wiki(wiki) if wiki else None
        skills = conns.get("skills.db")
        report["databases"]["skills"] = collect_skills(skills) if skills else None
        creds = conns.get("credentials.db")
        report["databases"]["credentials"] = collect_credentials(creds) if creds else None

        if mem:
            report["hashes"]["memories"] = _hash_sample(
                mem, "memories", "memory_id", "content", sample_memories
            )
        if wiki:
            report["hashes"]["wiki"] = _hash_sample(wiki, "wiki_pages", "page_id", "content", sample_wiki)
        if skills:
            content_col = "content" if "content" in _columns(skills, "skills") else "description"
            report["hashes"]["skills"] = _hash_sample(skills, "skills", "name", content_col, sample_skills)
    finally:
        for conn in conns.values():
            conn.close()

    for db_file in DB_FILES:
        report["files"][db_file] = _file_digest(data / db_file)

    if not (data / "agent_keys.json").is_file():
        warnings.append("agent_keys.json 未在数据目录（agent 配置基线需另采，见 §9.1）")

    return report


def run(
    data_dir: str | Path,
    out_dir: str | Path,
    *,
    sample_memories: int = 500,
    sample_wiki: int = 20,
    sample_skills: int = 20,
    label: str | None = None,
    redact_paths: bool = True,
) -> dict:
    """执行一次基线采集并写盘（JSON + Markdown），返回 report 字典。"""
    data = Path(data_dir)
    out = Path(out_dir)
    report = collect_report(
        data,
        sample_memories=sample_memories,
        sample_wiki=sample_wiki,
        sample_skills=sample_skills,
        label=label,
        redact_paths=redact_paths,
    )
    label = report["label"]
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"2.0-迁移基线-{label}.json"
    md_path = out / f"2.0-迁移基线-{label}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    report["_paths"] = {"json": str(json_path), "markdown": str(md_path)}
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SGME 2.0 迁移基线采集（只读，阶段 0）")
    ap.add_argument("--data-dir", default="data", help="数据目录（含 memory.db；默认 data）")
    ap.add_argument("--out-dir", default="docs/migration", help="报告输出目录（默认 docs/migration）")
    ap.add_argument("--sample-memories", type=int, default=500, help="记忆内容哈希抽样条数（默认 500）")
    ap.add_argument("--sample-wiki", type=int, default=20, help="知识库抽样条数（默认 20）")
    ap.add_argument("--sample-skills", type=int, default=20, help="技能抽样条数（默认 20）")
    ap.add_argument("--label", default=None, help="报告文件名标签（默认 UTC 日期 YYYYMMDD）")
    ap.add_argument(
        "--no-redact-paths",
        action="store_true",
        help="报告里写绝对数据目录（默认脱敏为目录名）",
    )
    args = ap.parse_args(argv)

    try:
        report = run(
            args.data_dir,
            args.out_dir,
            sample_memories=args.sample_memories,
            sample_wiki=args.sample_wiki,
            sample_skills=args.sample_skills,
            label=args.label,
            redact_paths=not args.no_redact_paths,
        )
    except FileNotFoundError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1

    mem = report["databases"].get("memory") or {}
    print(
        "[OK] 基线已写入："
        f"{report['_paths']['json']} / {report['_paths']['markdown']}；"
        f"memories={(mem.get('memories') or {}).get('total')} "
        f"archive={mem.get('memory_archive')} "
        f"wiki={(report['databases'].get('wiki') or {}).get('wiki_pages')} "
        f"skills={(report['databases'].get('skills') or {}).get('skills')}"
    )
    for wmsg in report["warnings"]:
        print(f"[WARN] {wmsg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
