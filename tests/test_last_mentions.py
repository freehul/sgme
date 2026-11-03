# -*- coding: utf-8 -*-
"""D-70 记忆回指块（last_mentions）测试。

设计要点（逐条对应 D-70）：
1. 候选只有 focus / status 两个维度，最近 7 天
2. 排除遗忘请求（D-68）与他人事实（D-66）
3. focus 优先于 status，同级按时间倒序
4. 预算超限截断到 1 条
5. **默认 off**（用户 10-30 拍板首月关闭），开关在 inject.last_mentions.enabled
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.operations import inject as inject_ops
from sgme.profile import last_mentions as lm_mod


@pytest.fixture
def env(tmp_path):
    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    yield mem, cfg
    for c in (mem, sess, wiki):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _add(conn, content: str, *, dim: str = "focus", subject: str = "self",
         memory_type: str = "fact", days_ago: int = 0) -> str:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return memory_dao.insert_memory(
        conn, content=content, memory_type=memory_type, priority=50,
        time_velocity="dynamic", ttl_days=None, dimension_ids=[dim],
        occurred_at=ts, prompt_version="test", updated_at=ts,
        extra={"type": "事实", "subject": subject},
    )


# ---------- 取数规则 ----------

def test_pick_only_focus_and_status(env):
    mem, _ = env
    _add(mem, "焦点事项", dim="focus")
    _add(mem, "状态事项", dim="status")
    _add(mem, "偏好事项", dim="preferences")   # 不得进入候选
    items = lm_mod.pick(mem, limit=5)
    contents = [i["content"] for i in items]
    assert "焦点事项" in contents
    assert "状态事项" in contents
    assert "偏好事项" not in contents


def test_pick_excludes_forget_and_others(env):
    mem, _ = env
    _add(mem, "本人焦点")
    _add(mem, "遗忘主题", memory_type="forget_request")
    _add(mem, "他人事实", subject="others")
    contents = [i["content"] for i in lm_mod.pick(mem, limit=5)]
    assert "本人焦点" in contents
    assert "遗忘主题" not in contents
    assert "他人事实" not in contents


def test_pick_prefers_focus_then_recency(env):
    mem, _ = env
    _add(mem, "较新的状态", dim="status", days_ago=0)
    _add(mem, "较旧的焦点", dim="focus", days_ago=3)
    items = lm_mod.pick(mem, limit=2)
    assert items[0]["content"] == "较旧的焦点"   # focus 优先于更新的 status


def test_pick_respects_lookback_window(env):
    mem, _ = env
    _add(mem, "很久以前的事", days_ago=30)
    assert lm_mod.pick(mem, days=7) == []


def test_char_budget_truncates_to_one(env):
    mem, _ = env
    long_text = "这是一条很长的焦点内容" * 8   # ≈ 88 字，单条就超预算
    _add(mem, long_text, days_ago=1)
    _add(mem, "第二条", days_ago=2)
    items = lm_mod.pick(mem, limit=2, char_budget=70)
    assert len(items) == 1


def test_render_format(env):
    mem, _ = env
    _add(mem, "周五要去医院复查", days_ago=1)
    items = lm_mod.pick(mem, limit=1)
    text = lm_mod.render(items)
    assert text.startswith("[上次提到] ")
    assert "周五要去医院复查" in text


# ---------- 注入接线：默认 off ----------

def test_inject_default_off(env):
    mem, cfg = env
    _add(mem, "焦点事项")
    assert cfg["inject"]["last_mentions"]["enabled"] is False
    res = inject_ops.inject(mem, cfg, mode="daily")
    assert res.ok
    assert not [b for b in res.data["blocks"] if b.get("kind") == "last_mentions"]


def test_inject_attaches_block_when_enabled(env):
    mem, cfg = env
    _add(mem, "周五要去医院复查", days_ago=1)
    cfg = {**cfg, "inject": {"last_mentions": {
        "enabled": True, "limit": 2, "days": 7, "char_budget": 70}}}
    res = inject_ops.inject(mem, cfg, mode="daily")
    assert res.ok
    blocks = [b for b in res.data["blocks"] if b.get("kind") == "last_mentions"]
    assert len(blocks) == 1
    assert blocks[0]["title"] == "上次提到"
    assert "周五要去医院复查" in blocks[0]["items"][0]["content"]
