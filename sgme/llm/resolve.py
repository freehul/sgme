# -*- coding: utf-8 -*-
"""llm/resolve.py：提炼 LLM 动态链构造（T-43，2026-08-13 用户定）。

策略（用户未指定 → 跟随 agent；指定 → agent 降备用）：
- ``refine.llm_override`` 非空（用户指定专用提炼 LLM）
  → [override 节点, agent 节点, 静态链剩余节点]
- override 为空 + session 有 agent_model（agent 声明，provider/model）
  → [agent 节点, 静态链剩余节点]
- agent_model 未声明 / provider 不在 providers 表
  → 原静态链（零破坏）

「直接复制其模型调用参数」= 从 providers 表复制连接参数
（base_url/api_key_env/context_window/超时等，密钥引用不落盘），
采样参数（temperature/thinking 等）用链默认或 override 自带。

⚠️ 2026-08-16 T-4x 修复：动态链重建时从静态链同 provider 节点继承采样参数
（max_tokens/sampling/extra_body）——否则 agent_model 动态链会丢失 llm.yaml
节点的 ``extra_body: {thinking: disabled}``，思考型模型（deepseek-v4-flash）
输出 reasoning_content、content 为空 → L1/L1.5 解析失败（DSH 会话 2456ee64
连续 12 次 error 的根因）。

⚠️ 模型选择立场（2026-08-13 用户定）：不建议引导用户使用本地模型
（向量维度不够 + 能力不够），但用户可自定义任何 OpenAI 兼容提供商——
「不建议 ≠ 不可以」。本模块不写死任何品牌倾向。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("sgme.llm.resolve")

# 动态链保留的静态兜底级数（agent 节点之后保留的静态链尾部，含 rule drop_batch）
# 2026-08-22：2→3——提炼链扩展为 zhipu→agnes→deepseek→drop_batch 三层兜底后，
# 只保留 2 层会把 agnes 免费层挤掉（agent_model 文件动态链丢失免费兜底）
FALLBACK_TAIL_KEEP = 3


def _parse_agent_model(agent_model: str) -> tuple[str, str] | None:
    """解析 ``provider/model`` → (provider, model)；非法返回 None。"""
    if not agent_model or "/" not in agent_model:
        return None
    provider, _, model = agent_model.partition("/")
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return None
    return provider, model


def _build_node(provider: str, model: str, providers: dict[str, Any],
                extra: dict[str, Any] | None = None,
                static_node: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """按 provider 查连接表构造链节点；provider 缺失返回 None。

    ``extra``：override 自带的节点参数（max_tokens/sampling/extra_body 等），
    覆盖 providers 表的连接默认（节点内联字段优先，与 llm.yaml 合并语义一致）。

    ``static_node``：静态链中同 provider 的节点（2026-08-16 T-4x 修复）——
    动态链重建时继承其**采样参数**（max_tokens/sampling/extra_body）。否则
    agent_model 动态链会丢失 llm.yaml 的 ``extra_body: {thinking: disabled}``
    等节点级配置，思考型模型（deepseek-v4-flash）输出 reasoning_content、
    content 为空 → L1/L1.5 解析失败（实锤：DSH 会话 2456ee64 连续 12 次 error）。
    """
    conn = providers.get(provider)
    if not conn:
        logger.warning("agent_model 声明的 provider 不在 providers 表: %s", provider)
        return None
    node: dict[str, Any] = {
        "provider": provider,
        "model": model,
    }
    # 复制连接参数（密钥只复制环境变量名引用，不落盘——铁律 #10）
    for k in ("base_url", "api_key_env", "provider_type", "context_window",
              "timeout_s", "max_retries", "health_endpoint", "health_interval_s"):
        if conn.get(k) is not None:
            node[k] = conn[k]
    # 继承静态链同 provider 节点的采样参数（2026-08-16 T-4x）：
    # max_tokens/sampling/extra_body 是模型行为关键（thinking 开关等），
    # 连接字段来自 providers 表，采样参数来自 llm.yaml 节点——两者本应合并。
    if static_node:
        for k in ("max_tokens", "sampling", "extra_body"):
            if static_node.get(k) is not None:
                node[k] = static_node[k]
    if extra:
        node.update(extra)  # override 内联字段优先（含 max_tokens/sampling/extra_body）
    return node


def resolve_refinement_chain(
    cfg: dict[str, Any],
    agent_model: str | None = None,
    override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """构造 refinement 链（T-43 策略语义，见模块 docstring）。

    Args:
        cfg: 运行时配置（llm.chains.refinement 静态链 + llm.providers 连接表 +
             refine.llm_override 用户指定）。
        agent_model: session 声明的 agent 模型（provider/model）；None 跳过。
        override: 用户指定的专用提炼 LLM 节点（refine.llm_override 内容）。

    Returns:
        完整链（含尾部 rule drop_batch 兜底）。
    """
    chains = (cfg.get("llm") or {}).get("chains") or cfg.get("chains") or {}
    static_chain = chains.get("refinement") or []
    if not static_chain:
        return static_chain

    # override 显式参数优先，否则从 cfg.refine.llm_override 读（调用方免传）
    if override is None:
        override = (cfg.get("refine") or {}).get("llm_override") or {}

    providers = (cfg.get("llm") or {}).get("providers") or {}
    # 静态链按 provider 索引（2026-08-16 T-4x：动态节点继承静态节点的采样参数）
    static_by_provider: dict[str, dict[str, Any]] = {}
    for n in static_chain:
        p = n.get("provider")
        if p and p not in static_by_provider:
            static_by_provider[p] = n

    agent_node: dict[str, Any] | None = None
    if agent_model:
        parsed = _parse_agent_model(agent_model)
        if parsed:
            provider, model = parsed
            agent_node = _build_node(provider, model, providers,
                                     static_node=static_by_provider.get(provider))

    # 静态链尾部兜底（保留 lm-studio + rule drop_batch 等既有尾部）
    tail = static_chain[-FALLBACK_TAIL_KEEP:] if len(static_chain) > 1 else static_chain

    dynamic: list[dict[str, Any]] = []
    if override and override.get("provider"):
        # 用户指定 → 专用为主
        ov_node = _build_node(
            override["provider"], override.get("model", ""), providers,
            extra={k: v for k, v in override.items() if k not in ("provider", "model")},
            static_node=static_by_provider.get(override["provider"]),
        )
        if ov_node:
            dynamic.append(ov_node)
        else:
            logger.warning("llm_override provider 不在 providers 表: %s，跳过指定", override.get("provider"))
    if agent_node:
        dynamic.append(agent_node)
    if not dynamic:
        return static_chain  # 无可动态节点 → 原静态链
    # 去重：动态节点与尾部 provider 重复时去掉尾部对应项
    dyn_providers = {n["provider"] for n in dynamic}
    tail = [n for n in tail if n.get("provider") not in dyn_providers]
    return dynamic + tail


def build_refinement_cfg(cfg: dict[str, Any], agent_model: str | None = None) -> dict[str, Any]:
    """返回带动态 refinement 链的 cfg 副本（提炼调用点用）。

    只替换 ``llm.chains.refinement``（或顶层 chains），其余配置原样共享；
    不修改入参（纯函数）。
    """
    override = (cfg.get("refine") or {}).get("llm_override") or {}
    chain = resolve_refinement_chain(cfg, agent_model=agent_model, override=override)
    import copy

    new_cfg = copy.deepcopy(cfg)
    if "llm" in new_cfg and isinstance(new_cfg.get("llm"), dict):
        new_cfg["llm"].setdefault("chains", {})["refinement"] = chain
    else:
        new_cfg["chains"] = dict(new_cfg.get("chains") or {})
        new_cfg["chains"]["refinement"] = chain
    return new_cfg
