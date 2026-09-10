"""L1 空结果重试：模型返回空数组时加提示重试一次（2026-09-11）。

背景：本地模型在 L1 提炼上存在「空结果」的静默丢失——输出 `[]`（completion_tokens≈2），
旧逻辑解析成功即 return 并记 status=ok，该块内容永久不入库。
实测（longmemeval q2 库）：l1_extraction 95 次中 12 次空结果（12.6%），
其中含答案所在块 → recall@8 归零。

修复口径：空数组视为可疑 → 加提示重试一次；仍空则正常返回（空块合法，不抛错）。
"""
from __future__ import annotations

import httpx
import pytest

from sgme import config as sgme_config
from sgme.engine import l1 as l1_mod


@pytest.fixture
def cfg():
    """加载真实配置（含 llm 链与维度注册表）。"""
    return sgme_config.load_config()


_MEM = {
    "content": "用户每天通勤单程 45 分钟",
    "dimensions": ["技术栈"],
    "memory_type": "persona",
    "priority": 70,
    "time_velocity": "static",
}
_MEM_BODY = __import__("json").dumps([_MEM], ensure_ascii=False)


def _client_seq(bodies: list[str]):
    """按顺序返回响应，并记录每次请求体（用于断言重试次数与提示词）。"""
    import json

    calls: list[str] = []
    state = {"i": 0}

    def handler(req):
        calls.append(req.content.decode("utf-8", "replace"))
        i = state["i"]
        state["i"] = i + 1
        body = bodies[min(i, len(bodies) - 1)]
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False), calls


def test_empty_then_memories_retries(cfg):
    """首次空数组 → 重试 → 第二次返回记忆：应拿到记忆，且确实调了 2 次。"""
    cli, calls = _client_seq(["[]", _MEM_BODY])
    memories, _, _ = l1_mod.extract_l1("用户说每天通勤 45 分钟", cfg["dimensions"], cfg["llm"], client=cli)
    assert len(calls) == 2, f"空结果应重试一次，实际调用 {len(calls)} 次"
    assert len(memories) == 1, "重试后应拿到记忆"
    assert "45" in memories[0]["content"]


def test_empty_twice_returns_empty_without_raise(cfg):
    """两次都空 → 正常返回空列表，不抛异常（空块合法）。"""
    cli, calls = _client_seq(["[]", "[]"])
    memories, _, _ = l1_mod.extract_l1("嗯嗯好的", cfg["dimensions"], cfg["llm"], client=cli)
    assert len(calls) == 2, f"空结果应重试且只重试一次，实际调用 {len(calls)} 次"
    assert memories == []


def test_nonempty_no_extra_call(cfg):
    """首次就有记忆 → 不重试。"""
    cli, calls = _client_seq([_MEM_BODY])
    memories, _, _ = l1_mod.extract_l1("用户说每天通勤 45 分钟", cfg["dimensions"], cfg["llm"], client=cli)
    assert len(calls) == 1, f"非空结果不应重试，实际调用 {len(calls)} 次"
    assert len(memories) == 1


def test_retry_prompt_adds_empty_hint(cfg):
    """重试时的提示词应带上「上次为空」的追加提示。"""
    cli, calls = _client_seq(["[]", _MEM_BODY])
    l1_mod.extract_l1("用户说每天通勤 45 分钟", cfg["dimensions"], cfg["llm"], client=cli)
    assert len(calls) == 2
    first, second = calls[0], calls[1]
    assert "上次" in second or "空" in second, "重试提示词应说明上次输出为空"
    assert second != first, "重试提示词必须与首次不同"
