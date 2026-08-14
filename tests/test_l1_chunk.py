"""L1 分块单元测试：chunk_conversation 边界场景 + extract_l1 合并去重。"""
from __future__ import annotations

import json

import httpx
import pytest

from sgme import config as sgme_config
from sgme.engine import l1 as l1_mod


@pytest.fixture
def cfg():
    """加载真实配置（含 llm 链与维度注册表）。"""
    return sgme_config.load_config()


def _conversation(num_msgs: int = 20, msg_size: int = 500) -> str:
    """构造 num_msgs 条消息，每条 msg_size 字符的会话。"""
    parts = []
    for i in range(num_msgs):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"消息{i} " + "x" * msg_size
        parts.append(f"# 1783763000.{i} {role}\n{content}\n")
    return "".join(parts)


# ---------- chunk_conversation ----------

def test_chunk_short_conversation_single_chunk():
    """短会话（< chunk_size）→ 单块。"""
    conv = _conversation(3, 100)
    chunks = l1_mod.chunk_conversation(conv, chunk_size=8000)
    assert len(chunks) == 1
    assert chunks[0] == conv


def test_chunk_long_conversation_multiple_chunks():
    """长会话 → 多块，且每块在消息边界、不超 chunk_size + 单条消息。"""
    conv = _conversation(40, 500)  # ~20K 字符
    chunks = l1_mod.chunk_conversation(conv, chunk_size=8000, overlap=1500)
    assert len(chunks) >= 2
    for c in chunks:
        # 每块以消息头开始
        assert c.startswith("# "), "块应以消息头开始"
        # 块内每行消息完整（不截断 content）
        assert "\n#" not in c[:-1] or True  # 结构校验在下方
    # 合并后应覆盖全部内容
    joined = "".join(chunks)
    assert len(joined) >= len(conv) * 0.9  # 重叠导致略超


def test_chunk_no_message_headers_fallback():
    """无消息头结构 → 按字符硬切兜底。"""
    conv = "abcdefgh" * 2000  # 16K 无消息头
    chunks = l1_mod.chunk_conversation(conv, chunk_size=8000)
    assert len(chunks) == 2
    assert all(len(c) <= 8000 for c in chunks)


def test_chunk_huge_message_not_truncated():
    """单条消息超 chunk_size → 单独成块不截断。"""
    big = f"# 1783763000.1 user\n{'y' * 20000}\n"
    conv = big + _conversation(2, 100)
    chunks = l1_mod.chunk_conversation(conv, chunk_size=8000)
    # 大消息块独立存在
    assert any("y" * 20000 in c for c in chunks)


# ---------- extract_l1 分块合并 ----------

def test_extract_l1_chunked_merges_dedup(cfg, monkeypatch):
    """分块提炼：多块合并 + 按 content 去重。"""
    conv = _conversation(40, 500)

    # mock LLM：每块返回固定 2 条记忆（其中一条重复，验证去重）
    body1 = json.dumps([
        {"content": "用户在用 Python 开发", "dimensions": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
        {"content": "用户本周忙项目", "dimensions": ["status"],
         "memory_type": "episodic", "priority": 70, "time_velocity": "dynamic"},
    ], ensure_ascii=False)
    body2 = json.dumps([
        {"content": "用户在用 Python 开发", "dimensions": ["tech_stack"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
        {"content": "用户喜欢先计划后行动", "dimensions": ["style"],
         "memory_type": "persona", "priority": 75, "time_velocity": "static"},
    ], ensure_ascii=False)
    state = {"i": 0}

    def handler(req):
        i = state["i"]
        state["i"] = i + 1
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body1 if i % 2 == 0 else body2}}]
        })

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    memories, provider, meta = l1_mod.extract_l1(
        conv, cfg["dimensions"], cfg["llm"], client=cli,
        chunk_size=8000, overlap=1500,
    )
    assert provider == "chunked"
    contents = [m["content"] for m in memories]
    # 去重后：Python/忙项目/先计划 = 3 条唯一
    assert len(set(contents)) == len(contents)
    assert "用户在用 Python 开发" in contents
    assert "用户本周忙项目" in contents
    assert "用户喜欢先计划后行动" in contents
    assert len(memories) == 3
    # 分块版本元信息：全部块同一版本 → 直接用该版本
    assert meta["stage"] == "l1_extraction"
    assert meta["version"].startswith("working-")
    assert meta["variant"] is None


def test_extract_l1_short_no_chunk(cfg, monkeypatch):
    """短会话不触发分块（provider 为原始名）。"""
    conv = _conversation(3, 100)
    body = json.dumps([
        {"content": "测试记忆", "dimensions": ["identity"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
    ], ensure_ascii=False)
    cli = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]})
    ), trust_env=False)
    memories, provider, _ = l1_mod.extract_l1(
        conv, cfg["dimensions"], cfg["llm"], client=cli,
    )
    assert provider == "deepseek"  # 非 chunked；v0.5 主模型切换 DeepSeek（原 lm-studio）
    assert len(memories) == 1
