# -*- coding: utf-8 -*-
"""llm/resolve.py 动态提炼链测试（T-43）。

三态：override 指定（agent 降备用）/ agent 跟随 / provider 缺失回退。
"""

from __future__ import annotations

from sgme.llm import resolve as resolve_mod


def _base_cfg() -> dict:
    """最小配置：静态链（deepseek 主 → lm-studio 备 → rule 兜底）+ providers 表。"""
    return {
        "llm": {
            "chains": {
                "refinement": [
                    {"provider": "deepseek", "model": "deepseek-v4-flash"},
                    {"provider": "lm-studio", "model": "qwen/qwen3.5-9b"},
                    {"provider": "rule", "rule": "drop_batch"},
                ],
            },
            "providers": {
                "deepseek": {"base_url": "https://api.deepseek.com/v1",
                             "api_key_env": "DEEPSEEK_API_KEY", "context_window": 1048576},
                "lm-studio": {"base_url": "http://127.0.0.1:1014/v1",
                              "api_key_env": "", "context_window": 65536},
            },
        },
        "refine": {"llm_override": {}},
    }


def test_follows_agent_model_without_override():
    """用户未指定 → 链首 = agent 声明模型（复制 providers 连接参数）。"""
    cfg = _base_cfg()
    chain = resolve_mod.resolve_refinement_chain(cfg, agent_model="deepseek/deepseek-v4-flash")
    assert chain[0]["provider"] == "deepseek"
    assert chain[0]["model"] == "deepseek-v4-flash"
    assert chain[0]["base_url"] == "https://api.deepseek.com/v1"  # 连接参数复制
    assert chain[0]["api_key_env"] == "DEEPSEEK_API_KEY"  # 密钥引用不落盘
    assert chain[-1]["rule"] == "drop_batch"  # 尾部兜底保留


def test_override_takes_priority_agent_fallback():
    """用户指定 llm_override → 专用为主，agent 模型为备用。"""
    cfg = _base_cfg()
    cfg["refine"]["llm_override"] = {"provider": "lm-studio", "model": "qwen/qwen3.5-9b"}
    chain = resolve_mod.resolve_refinement_chain(
        cfg, agent_model="deepseek/deepseek-v4-flash")
    assert chain[0]["provider"] == "lm-studio"  # 用户指定优先
    assert chain[1]["provider"] == "deepseek"   # agent 模型降为备用
    assert chain[1]["model"] == "deepseek-v4-flash"


def test_unknown_provider_skipped():
    """agent_model 的 provider 不在 providers 表 → 跳过该节点，回退静态链。"""
    cfg = _base_cfg()
    chain = resolve_mod.resolve_refinement_chain(cfg, agent_model="ghost/ghost-model")
    assert chain[0]["provider"] == "deepseek"  # 原静态链
    assert chain[1]["provider"] == "lm-studio"


def test_no_agent_model_static_chain():
    """未声明 agent_model → 原静态链（零破坏）。"""
    cfg = _base_cfg()
    chain = resolve_mod.resolve_refinement_chain(cfg, agent_model=None)
    assert [n["provider"] for n in chain] == ["deepseek", "lm-studio", "rule"]


def test_bad_agent_model_format_ignored():
    """agent_model 格式非法（无 '/'）→ 忽略，回退静态链。"""
    cfg = _base_cfg()
    chain = resolve_mod.resolve_refinement_chain(cfg, agent_model="deepseek-v4-flash")
    assert chain[0]["provider"] == "deepseek"


def test_override_unknown_provider_static_fallback():
    """override 的 provider 不存在 → 跳过指定，仍跟随 agent（若有）。"""
    cfg = _base_cfg()
    cfg["refine"]["llm_override"] = {"provider": "no-such", "model": "x"}
    chain = resolve_mod.resolve_refinement_chain(cfg, agent_model="deepseek/deepseek-v4-flash")
    assert chain[0]["provider"] == "deepseek"  # agent 模型


def test_build_refinement_cfg_pure():
    """build_refinement_cfg 不修改入参 cfg（纯函数）。"""
    cfg = _base_cfg()
    before = cfg["llm"]["chains"]["refinement"][0]["model"]
    new_cfg = resolve_mod.build_refinement_cfg(cfg, agent_model="deepseek/deepseek-v4-flash")
    assert cfg["llm"]["chains"]["refinement"][0]["model"] == before  # 原 cfg 未变
    assert new_cfg["llm"]["chains"]["refinement"][0]["model"] == "deepseek-v4-flash"
