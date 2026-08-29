"""T6 测试：模板引擎（加载 + 校验 + 继承 + 查询 + 注入）。

- load_template 加载 4 个预定义模板
- validate 拒绝越界 section / limit 超范围 / token 预算超限
- extends 继承展开
- query_section：TTL 过滤 / 排序 / time_window
- build_inject_blocks：blocks + stats
- Tier0 降级：摘要不存在 → present:false
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from sgme import config
from sgme.profile import inject, template
from sgme.data import db as db_mod, memory_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def templates_dir(tmp_path, monkeypatch):
    """隔离 templates 目录到 tmp_path（用于写测试用模板）。"""
    td = tmp_path / "templates"
    td.mkdir()
    # 复制真实模板到 tmp
    src = config.PROJECT_ROOT / "templates"
    for f in src.glob("*.yaml"):
        (td / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(template, "TEMPLATES_DIR", td)
    return td


# ---------- 加载 4 个预定义模板 ----------

def test_load_daily_template(cfg):
    t = template.load_template("daily", cfg["dimensions"])
    assert t["name"] == "daily"
    assert t["display_name"] == "日常模式"
    assert "identity" in t["memory_types"]
    assert len(t["sections"]) == 3


def test_load_coding_template(cfg):
    t = template.load_template("coding", cfg["dimensions"])
    assert t["name"] == "coding"
    assert "tech_stack" in t["memory_types"]


def test_load_work_template(cfg):
    t = template.load_template("work", cfg["dimensions"])
    assert t["name"] == "work"


def test_load_full_template(cfg):
    t = template.load_template("full", cfg["dimensions"])
    assert t["name"] == "full"
    assert len(t["memory_types"]) == 13  # 全量（2026-08-18 三池重构移除 projects/tasks，15→13；ideas 不入注入模板）


def test_load_template_not_found_raises():
    with pytest.raises(template.TemplateError, match="不存在"):
        template.load_template("nonexistent")


# ---------- 校验：越界 section ----------

def test_validate_rejects_dimension_outside_memory_types(cfg):
    """section.dimensions 含 memory_types 外的维度 → TemplateError。"""
    bad = {
        "name": "bad",
        "display_name": "坏模板",
        "memory_types": ["identity", "family"],
        "token_budget": 700,
        "sections": [
            {"title": "x", "query": {"dimensions": ["tech_stack"], "limit": 5}},
        ],
    }
    with pytest.raises(template.TemplateError, match="越界"):
        template.validate_template(bad, cfg["dimensions"])


def test_validate_rejects_unregistered_dimension(cfg):
    """section.dimensions 含未注册维度 → TemplateError。"""
    bad = {
        "name": "bad",
        "memory_types": ["unknown_dim"],
        "token_budget": 700,
        "sections": [
            {"title": "x", "query": {"dimensions": ["unknown_dim"], "limit": 5}},
        ],
    }
    with pytest.raises(template.TemplateError, match="未注册"):
        template.validate_template(bad, cfg["dimensions"])


# ---------- 校验：limit 范围 ----------

def test_validate_rejects_limit_zero(cfg):
    bad = {
        "name": "bad", "memory_types": ["identity"], "token_budget": 700,
        "sections": [{"title": "x", "query": {"dimensions": ["identity"], "limit": 0}}],
    }
    with pytest.raises(template.TemplateError, match="limit"):
        template.validate_template(bad, cfg["dimensions"])


def test_validate_rejects_limit_over_50(cfg):
    bad = {
        "name": "bad", "memory_types": ["identity"], "token_budget": 700,
        "sections": [{"title": "x", "query": {"dimensions": ["identity"], "limit": 100}}],
    }
    with pytest.raises(template.TemplateError, match="limit"):
        template.validate_template(bad, cfg["dimensions"])


# ---------- 校验：token 预算 ----------

def test_validate_rejects_token_budget_exceeded(cfg):
    """token 预算超限：Σ(limit)×30 > token_budget → 拒绝。"""
    bad = {
        "name": "bad", "memory_types": ["identity", "family"],
        "token_budget": 100,  # 故意设小
        "sections": [
            {"title": "a", "query": {"dimensions": ["identity"], "limit": 5}},  # 5×30=150 > 100
        ],
    }
    with pytest.raises(template.TemplateError, match="token 预算超限"):
        template.validate_template(bad, cfg["dimensions"])


def test_validate_accepts_within_budget(cfg):
    good = {
        "name": "good", "memory_types": ["identity"],
        "token_budget": 200,
        "sections": [
            {"title": "a", "query": {"dimensions": ["identity"], "limit": 5}},  # 150 ≤ 200
        ],
    }
    template.validate_template(good, cfg["dimensions"])


# ---------- 校验：match / sort ----------

def test_validate_rejects_invalid_match(cfg):
    bad = {
        "name": "bad", "memory_types": ["identity"], "token_budget": 700,
        "sections": [{"title": "x", "query": {"dimensions": ["identity"], "match": "invalid", "limit": 5}}],
    }
    with pytest.raises(template.TemplateError, match="match"):
        template.validate_template(bad, cfg["dimensions"])


def test_validate_rejects_invalid_sort(cfg):
    bad = {
        "name": "bad", "memory_types": ["identity"], "token_budget": 700,
        "sections": [{"title": "x", "query": {"dimensions": ["identity"], "sort": "invalid", "limit": 5}}],
    }
    with pytest.raises(template.TemplateError, match="sort"):
        template.validate_template(bad, cfg["dimensions"])


# ---------- time_window 解析 ----------

def test_parse_time_window_days():
    n, unit = template._parse_time_window("updated_at > 30d")
    assert n == 30 and unit == "d"


def test_parse_time_window_hours():
    n, unit = template._parse_time_window("updated_at > 6h")
    assert n == 6 and unit == "h"


def test_parse_time_window_weeks():
    n, unit = template._parse_time_window("updated_at > 2w")
    assert n == 2 and unit == "w"


def test_parse_time_window_invalid_raises():
    with pytest.raises(template.TemplateError, match="语法错误"):
        template._parse_time_window("invalid format")


def test_time_window_to_threshold_returns_iso():
    ts = template.time_window_to_threshold("updated_at > 30d")
    assert ts.endswith("Z")
    # 应是 30 天前附近
    from datetime import datetime, timezone
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - t
    assert 29 <= delta.days <= 31


# ---------- extends 继承 ----------

def test_extends_merges_sections(templates_dir):
    """extends: base sections 在前，子按 title 覆盖，新 section 追加。"""
    base = {
        "name": "base", "display_name": "基础",
        "memory_types": ["identity", "family"],
        "token_budget": 500,
        "sections": [
            {"title": "A", "query": {"dimensions": ["identity"], "limit": 3}},
            {"title": "B", "query": {"dimensions": ["family"], "limit": 3}},
        ],
    }
    child = {
        "name": "child", "display_name": "子模板",
        "extends": "base",
        "memory_types": ["identity", "family", "status"],
        "token_budget": 600,
        "sections": [
            {"title": "B", "query": {"dimensions": ["family"], "limit": 5}},  # 覆盖
            {"title": "C", "query": {"dimensions": ["status"], "limit": 3}},  # 追加
        ],
    }
    (templates_dir / "base.yaml").write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    (templates_dir / "child.yaml").write_text(yaml.safe_dump(child, allow_unicode=True), encoding="utf-8")

    # 需要 dimensions 参数校验
    cfg = config.load_config()
    t = template.load_template("child", cfg["dimensions"])
    assert t["memory_types"] == ["identity", "family", "status"]
    titles = [s["title"] for s in t["sections"]]
    # base 在前，子覆盖 B，追加 C
    assert titles == ["A", "B", "C"]
    # B 被子覆盖（limit=5）
    b_section = [s for s in t["sections"] if s["title"] == "B"][0]
    assert b_section["query"]["limit"] == 5


def test_extends_chain_limit(templates_dir):
    """extends 链单层：base 自身 extends → 拒绝。"""
    # grandbase 自身 extends greatgrandbase → 构成 2 级链，加载 base 时应拒绝
    grandbase = {"name": "grandbase", "extends": "greatgrandbase", "memory_types": ["identity"], "token_budget": 500,
                 "sections": [{"title": "x", "query": {"dimensions": ["identity"], "limit": 3}}]}
    base = {"name": "base", "extends": "grandbase", "memory_types": ["identity"], "token_budget": 500,
            "sections": [{"title": "y", "query": {"dimensions": ["identity"], "limit": 3}}]}
    (templates_dir / "grandbase.yaml").write_text(yaml.safe_dump(grandbase), encoding="utf-8")
    (templates_dir / "base.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    cfg = config.load_config()
    with pytest.raises(template.TemplateError, match="单层"):
        template.load_template("base", cfg["dimensions"])


# ---------- query_section ----------

def test_query_section_ttl_filter_excludes_expired(mem_conn, cfg):
    """TTL 过滤：过期状态记忆不出现。"""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    memory_dao.insert_memory(
        mem_conn, content="过时状态", memory_type="persona",
        priority=80, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"], created_at=old, updated_at=old,
    )
    memory_dao.insert_memory(
        mem_conn, content="新鲜状态", memory_type="persona",
        priority=70, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"],
    )
    section = {"title": "状态", "query": {"dimensions": ["status"], "ttl_filter": True, "limit": 10}}
    results = inject.query_section(mem_conn, section, cfg["dimensions"])
    contents = {r["content"] for r in results}
    assert "新鲜状态" in contents
    assert "过时状态" not in contents


def test_query_section_static_sorts_by_priority(mem_conn, cfg):
    """静态维度默认 priority DESC。"""
    memory_dao.insert_memory(
        mem_conn, content="低优先级", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    memory_dao.insert_memory(
        mem_conn, content="高优先级", memory_type="persona",
        priority=90, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    section = {"title": "身份", "query": {"dimensions": ["identity"], "limit": 10}}
    results = inject.query_section(mem_conn, section, cfg["dimensions"])
    # priority DESC：高优先级在前
    assert results[0]["content"] == "高优先级"
    assert results[1]["content"] == "低优先级"


def test_query_section_dynamic_sorts_by_updated_at(mem_conn, cfg):
    """动态维度默认 updated_at DESC。"""
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    memory_dao.insert_memory(
        mem_conn, content="旧状态", memory_type="persona",
        priority=80, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"], created_at=old, updated_at=old,
    )
    memory_dao.insert_memory(
        mem_conn, content="新状态", memory_type="persona",
        priority=60, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"], created_at=now, updated_at=now,
    )
    section = {"title": "状态", "query": {"dimensions": ["status"], "limit": 10}}
    results = inject.query_section(mem_conn, section, cfg["dimensions"])
    # updated_at DESC：新状态在前（即使 priority 低）
    assert results[0]["content"] == "新状态"


def test_query_section_time_window_filters(mem_conn, cfg):
    """time_window='updated_at > 30d' 过滤生效。"""
    old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    memory_dao.insert_memory(
        mem_conn, content="60天前习惯", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["habits"], created_at=old, updated_at=old,
    )
    memory_dao.insert_memory(
        mem_conn, content="近期习惯", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["habits"],
    )
    section = {
        "title": "习惯",
        "query": {"dimensions": ["habits"], "time_window": "updated_at > 30d", "limit": 10},
    }
    results = inject.query_section(mem_conn, section, cfg["dimensions"])
    contents = {r["content"] for r in results}
    assert "近期习惯" in contents
    assert "60天前习惯" not in contents


def test_query_section_priority_min_filters(mem_conn, cfg):
    """priority_min 过滤。"""
    memory_dao.insert_memory(
        mem_conn, content="低", memory_type="persona",
        priority=40, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    memory_dao.insert_memory(
        mem_conn, content="高", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    section = {
        "title": "身份",
        "query": {"dimensions": ["identity"], "priority_min": 70, "limit": 10},
    }
    results = inject.query_section(mem_conn, section, cfg["dimensions"])
    contents = {r["content"] for r in results}
    assert "高" in contents
    assert "低" not in contents


def test_query_section_match_all(mem_conn, cfg):
    """match=all：必须同时命中所有维度。"""
    memory_dao.insert_memory(
        mem_conn, content="双标签", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["identity", "family"],
    )
    memory_dao.insert_memory(
        mem_conn, content="单标签", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    section = {
        "title": "x",
        "query": {"dimensions": ["identity", "family"], "match": "all", "limit": 10},
    }
    results = inject.query_section(mem_conn, section, cfg["dimensions"])
    contents = {r["content"] for r in results}
    assert contents == {"双标签"}


# ---------- build_inject_blocks ----------

def test_build_inject_blocks_structure(cfg):
    """注入响应含 blocks[] 与 stats。"""
    t = {
        "name": "test", "sections": [
            {"title": "段1", "query": {"dimensions": ["identity"], "limit": 5}},
        ],
    }
    section_results = [
        [{"content": "用户是开发者", "memory_id": "m1", "updated_at": None, "priority": 80}],
    ]
    result = inject.build_inject_blocks(t, section_results)
    assert "blocks" in result
    assert "stats" in result
    assert result["stats"]["mode"] == "test"
    assert result["stats"]["queries"] == 1
    assert result["stats"]["tokens_est"] > 0
    assert result["blocks"][0]["title"] == "段1"
    assert result["blocks"][0]["items"][0]["content"] == "用户是开发者"


def test_build_inject_blocks_tier0_absent_present_false(cfg):
    """Tier0 摘要不存在 → 静态维度直出（stats.tier0_present=False）。"""
    t = {"name": "test", "sections": [{"title": "x", "query": {"dimensions": ["identity"], "limit": 5}}]}
    result = inject.build_inject_blocks(t, [[]], tier0_summary=None)
    assert result["stats"]["tier0_present"] is False
    # 无 tier0 摘要 block
    assert all(b["title"] != "画像摘要" for b in result["blocks"])


def test_build_inject_blocks_tier0_present(cfg):
    """Tier0 摘要存在 → 第一个 block 是画像摘要。"""
    t = {"name": "test", "sections": [{"title": "x", "query": {"dimensions": ["identity"], "limit": 5}}]}
    result = inject.build_inject_blocks(t, [[]], tier0_summary="用户摘要文本")
    assert result["stats"]["tier0_present"] is True
    assert result["blocks"][0]["title"] == "画像摘要"
    assert result["blocks"][0]["present"] is True


def test_build_inject_blocks_relative_time(mem_conn, cfg):
    """updated_at < 30 天的条目附相对时间。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t = {"name": "test", "sections": [{"title": "x", "query": {"dimensions": ["status"], "limit": 5}}]}
    results = [{"content": "新状态", "memory_id": "m1", "updated_at": now, "priority": 70}]
    result = inject.build_inject_blocks(t, [results])
    item = result["blocks"][0]["items"][0]
    assert "relative_time" in item
    assert item["relative_time"] is not None


# ---------- inject 端到端 ----------

def test_inject_daily_template(mem_conn, cfg):
    """daily 模板端到端注入。"""
    # 插入一些数据
    memory_dao.insert_memory(
        mem_conn, content="用户是独立开发者", memory_type="persona",
        priority=90, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    memory_dao.insert_memory(
        mem_conn, content="用户用 Python 3.11", memory_type="persona",
        priority=85, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],  # daily 模板含 identity
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    memory_dao.insert_memory(
        mem_conn, content="当前忙 SGME", memory_type="persona",
        priority=70, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"], created_at=now, updated_at=now,
    )

    t = template.load_template("daily", cfg["dimensions"])
    result = inject.inject(mem_conn, t, cfg["dimensions"])
    assert "blocks" in result
    assert len(result["blocks"]) == 3  # daily 3 个 section
    # 至少有一个 block 有内容
    has_content = any(b["present"] for b in result["blocks"])
    assert has_content
    # stats 完整
    assert result["stats"]["mode"] == "daily"
    assert result["stats"]["queries"] == 3
