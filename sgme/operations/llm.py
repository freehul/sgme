"""operations/llm.py：LLM 供应商与降级链管理操作（v0.7 §7 三段式样板）。

只读信息超集，供 WebUI「供应商与降级链」页消费：
- ``llm_status``：降级链结构（chains）+ 链级规则（rules）+ 各供应商连接信息（providers）
- ``llm_health``：逐供应商健康探测（GET {base_url}{health_endpoint}，robust 不抛）

数据源：``cfg["llm"]``（运行时配置，由 ``sgme.config.load_llm_config`` 合并
providers.yaml 连接字段生成）。本模块**只读**，不承载任何写操作——
供应商/链属程序资源（llm.yaml/providers.yaml），由接口公开但不由接口改。

健康探测铁律（对齐 llm/provider.py）：
- httpx 客户端必须 ``trust_env=False``（防代理劫持 localhost）
- 探测永不抛异常：任何失败 → available=False + 原因（健康检查必须健壮）
- 探测函数可注入（``probe`` 参数），测试用假探测避免真实网络调用
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from sgme.config import (
    DEFAULT_PROVIDERS_CONFIG,
    load_embeddings_config,
    load_providers_config,
    persist_config,
    write_llm_config,
    write_providers_config,
)
from sgme.llm.provider import make_client
from sgme.operations.errors import InvalidArgs, OperationError, OperationResult

logger = logging.getLogger("sgme.operations.llm")

# 连接层默认健康端点（OpenAI 兼容标准；providers.yaml 可用 health_endpoint 覆盖）
_DEFAULT_HEALTH_ENDPOINT = "/models"
# 健康探测超时（秒）：只做连通性探测，不参与正常 LLM 调用超时
_PROBE_TIMEOUT_S = 5.0
# 供应商连接字段（llm_status 投影 / 供应商管理添删共用）
# vector_capable（T-44 统一供应商模型）：该供应商是否同时用作向量（embedding）
_PROVIDER_FIELDS = (
    "provider", "model", "base_url", "api_key_env", "context_window",
    "timeout_s", "health_endpoint", "display_name", "models", "vector_capable",
)

# ---------- 模型 Key 缺失检测（T-53 2026-08-18：免费托底新用户引导） ----------

# 统一提醒文案（health / inject 共用；{missing} 占位符由调用方填充）
MODEL_KEY_MISSING_NOTICE = (
    "SGME 模型配置缺失（{missing}）：LLM 提炼 / 向量检索将降级。"
    "申请免费 Key：智谱 GLM-4.7-Flash（https://open.bigmodel.cn 手机号注册，永久免费）→ ZHIPU_API_KEY；"
    "硅基流动 bge-m3（https://cloud.siliconflow.cn 实名后免费）→ SILICONFLOW_API_KEY。"
    "完整流程见 docs/guide/免费模型Key申请指南.md"
)


def detect_missing_model_keys(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """检测提炼链与向量端点的模型 Key 缺失（T-53：新用户引导）。

    遍历 refinement 链节点（rule 除外）+ search.vector，检查 api_key_env
    引用的环境变量是否缺失/为空。**只报告实际会用到**的缺失——未上链的
    供应商不检测（避免噪音）；rule 节点无 key 语义跳过。

    Returns:
        [{purpose, provider, model, key_env}, ...]；空列表 = 全部就绪。
    """
    missing: list[dict[str, str]] = []
    llm = cfg.get("llm") or {}
    chains = llm.get("chains") or cfg.get("chains") or {}
    for node in chains.get("refinement", []):
        if node.get("provider") == "rule":
            continue
        key_env = node.get("api_key_env") or ""
        if key_env and not os.environ.get(key_env):
            missing.append({
                "purpose": "refinement",
                "provider": str(node.get("provider", "")),
                "model": str(node.get("model", "")),
                "key_env": key_env,
            })
    vec = (cfg.get("search") or {}).get("vector") or {}
    vec_env = vec.get("api_key_env") or ""
    if vec.get("enabled", True) and vec_env and not os.environ.get(vec_env):
        missing.append({
            "purpose": "vector",
            "provider": str(vec.get("provider", "")),
            "model": str(vec.get("model", "")),
            "key_env": vec_env,
        })
    return missing


def model_keys_notice(cfg: dict[str, Any]) -> str:
    """缺失 Key 的统一提醒文案；空字符串 = Key 齐全（零噪音，不提示）。"""
    missing = detect_missing_model_keys(cfg)
    if not missing:
        return ""
    desc = "、".join(
        f"{m['key_env']}（{'LLM 提炼' if m['purpose'] == 'refinement' else '向量检索'}）"
        for m in missing
    )
    return MODEL_KEY_MISSING_NOTICE.format(missing=desc)


def _file_providers_with_flags(providers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """给 providers.yaml 连接表补充展示字段（vector_capable/models 等）。

    - 只读、只复制连接字段（含 api_key_env 环境变量名，铁律 #10 禁明文）
    - vector_capable：文件未显式定义时默认 False（非向量）
    """
    out: dict[str, dict[str, Any]] = {}
    for name, p in providers.items():
        out[name] = {
            "provider": name,
            **{k: p.get(k) for k in _PROVIDER_FIELDS if k != "provider" and k in p},
            "vector_capable": bool(p.get("vector_capable", False)),
        }
    return out


def _collect_vector_capable() -> dict[str, dict[str, Any]]:
    """统一供应商模型（T-44）：返回 vector_capable=true 的供应商连接信息。

    替代/兼容旧 embedding 段：向量提供商从带「向量」标签的统一供应商中选。
    """
    return _file_providers_with_flags(load_providers_config())


def _collect_providers(chains: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从各链节点提取**去重后**的非 rule 供应商连接信息。

    连接字段已由 ``sgme.config.load_llm_config`` 注入链节点（base_url/
    api_key_env/context_window/timeout_s...）。同一供应商在多条链出现时，
    取首个节点信息（连接字段一致，取首即可）。
    """
    out: dict[str, dict[str, Any]] = {}
    for nodes in (chains or {}).values():
        for node in nodes or []:
            name = node.get("provider")
            if not name or name == "rule" or name in out:
                continue
            out[name] = {k: node.get(k) for k in _PROVIDER_FIELDS if k in node}
            out[name]["vector_capable"] = bool(out[name].get("vector_capable", False))
    return out


def _collect_embeddings() -> dict[str, dict[str, Any]]:
    """读取 providers.yaml 顶层 embedding 段（向量提供商，T-43 兼容保留）。

    只读、只复制连接字段（含 api_key_env 环境变量名，铁律 #10 禁明文）。
    统一供应商模型（T-44）后本段不再是主入口，仅作向后兼容；新供应商
    一律走 providers 段 + vector_capable 标记。
    """
    out: dict[str, dict[str, Any]] = {}
    for name, p in load_embeddings_config().items():
        out[name] = {"provider": name, **{k: p.get(k) for k in (
            "display_name", "base_url", "api_key_env", "default_model", "models",
            "timeout_s", "max_retries",
        ) if k in p}}
        out[name]["vector_capable"] = True
    return out


def _probe_provider(info: dict[str, Any]) -> dict[str, Any]:
    """探测单个供应商连通性（GET {base_url}{health_endpoint}）。

    - 永不抛异常：任何失败 → available=False + 原因（截断到 200 字符）
    - 返回 available / error / latency_ms
    """
    base = (info.get("base_url") or "").rstrip("/")
    if not base:
        return {"available": False, "error": "缺 base_url"}
    path = info.get("health_endpoint") or _DEFAULT_HEALTH_ENDPOINT
    key_env = info.get("api_key_env")
    key = os.environ.get(key_env) if key_env else None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    timeout_s = float(info.get("timeout_s") or _PROBE_TIMEOUT_S)
    timeout_s = min(timeout_s, _PROBE_TIMEOUT_S)  # 探测只给短超时，不拖长
    t0 = time.monotonic()
    try:
        with make_client(timeout_s=timeout_s) as cli:
            resp = cli.get(f"{base}{path}", headers=headers)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code < 500:
            return {"available": True, "latency_ms": latency_ms}
        return {"available": False, "error": f"HTTP {resp.status_code}", "latency_ms": latency_ms}
    except Exception as e:  # noqa: BLE001 —— 健康探测必须健壮，禁止上抛
        return {"available": False, "error": str(e)[:200]}


def llm_status(cfg: dict[str, Any]) -> OperationResult:
    """读取降级链结构 + 链级规则 + 供应商连接信息（只读）。

    providers = 链节点非 rule 供应商 ∪ providers.yaml 连接表（后者补上未被
    任何链引用的供应商，供供应商管理页独立维护连接层）。
    """
    llm = cfg.get("llm") or {}
    chains = llm.get("chains") or {}
    providers = _collect_providers(chains)
    # 统一供应商模型（T-44）：以 providers.yaml 连接表为真相源，补上未被链引用的
    # 供应商 + 每个供应商的 vector_capable/models 标记（含嵌入向量段合并进来的）
    file_providers = _file_providers_with_flags(load_providers_config())
    for name, p in file_providers.items():
        if name in providers:
            providers[name].update({k: v for k, v in p.items() if k != "provider"})
        else:
            providers[name] = p
    # 向后兼容：旧 embedding 段中的向量提供商并入 providers（避免已配置向量丢失视图）
    for name, p in _collect_embeddings().items():
        if name not in providers:
            providers[name] = p
    # 当前生效向量 provider
    vector_cfg = (cfg.get("search") or {}).get("vector") or {}
    return OperationResult.succeed({
        "chains": chains,
        "rules": llm.get("rules") or {},
        "providers": providers,
        "embedding": _collect_embeddings(),
        "vector_current": vector_cfg.get("provider") or "",
    })


def llm_embedding_set_active(cfg: dict[str, Any], provider: str) -> OperationResult:
    """切换当前向量提供商（T-44 统一供应商模型，写回 search.vector）。

    - 校验 provider 必须**带「向量」标签**（vector_capable=true，统一供应商模型）
      ——兼容旧 embedding 段（旧向量提供商仍可选）
    - 切换后把该 provider 的连接字段（base_url/api_key_env/default_model）写入
      cfg["search"]["vector"]，并 ``persist_config`` 落盘（search 段在 CONFIG_SECTIONS）
    - 幂等：重复设为同一 provider 不报错
    """
    name = (provider or "").strip()
    if not name:
        raise InvalidArgs("向量提供商名不能为空")
    # 统一供应商模型：优先从 vector_capable=true 的统一供应商中找
    target = _file_providers_with_flags(load_providers_config()).get(name)
    if target is None or not target.get("vector_capable"):
        # 兼容旧 embedding 段
        target = _collect_embeddings().get(name)
    if target is None:
        raise OperationError(
            f"供应商 {name!r} 不存在或未标记为向量模型（请在供应商中添加并勾选「向量模型」）",
            error_code="ERR_NOT_FOUND",
        )
    search = cfg.setdefault("search", {})
    vector = search.setdefault("vector", {})
    vector["provider"] = name
    if target.get("base_url"):
        vector["base_url"] = target["base_url"]
    if target.get("api_key_env"):
        vector["api_key_env"] = target["api_key_env"]
    # 向量模型：优先取 models 首个，否则 default_model
    model = target.get("model") or (target.get("models") or [None])[0] or target.get("default_model")
    if model and target.get("models"):
        model = target["models"][0]
    if model:
        vector["model"] = model
    vector.setdefault("enabled", True)
    persist_config(cfg)
    return OperationResult.succeed({
        "provider": name,
        "vector": vector,
    })


def llm_provider_add(cfg: dict[str, Any], provider: str, payload: dict[str, Any]) -> OperationResult:
    """新增/更新供应商连接信息（写回 providers.yaml，密钥只存环境变量名）。

    - 校验：provider 非空、base_url 必填、api_key_env 必填（铁律 #10：禁明文 key）
    - 更新语义：与现有连接字段 merge，未传字段保留现值（幂等）
    - 写回前二次校验禁止写入明文 key 值（api_key_env 分支）
    """
    name = (provider or "").strip()
    if not name:
        raise InvalidArgs("供应商名不能为空")
    if not isinstance(payload, dict):
        raise InvalidArgs("payload 必须是对象")
    # 明文 key 铁律：api_key_env 只接受环境变量名，禁止 Bearer/sk- 等明文
    if "api_key" in payload or "key" in payload or "secret" in payload:
        raise InvalidArgs("禁止写入明文密钥：只允许 api_key_env 环境变量名")
    providers = load_providers_config() or {}
    existing = providers.get(name, {})
    merged: dict[str, Any] = {**existing}
    for k, v in payload.items():
        # vector_capable 是布尔：False 也要写入（区别于空串/None 跳过）
        if k == "vector_capable":
            merged[k] = bool(v)
            continue
        if v is None or v == "":
            continue
        merged[k] = v
    if not merged.get("base_url"):
        raise InvalidArgs("base_url 必填")
    if not merged.get("api_key_env"):
        raise InvalidArgs("api_key_env 必填（铁律 #10：只存环境变量名，禁止明文 key）")
    providers[name] = merged
    write_providers_config(providers)
    # 同步运行时 cfg：update 场景下把新连接字段注入链中已引用该供应商的节点
    # （vector_capable 属连接层标记，不注入链节点）
    _sync_provider_into_chains(cfg, name, merged)
    return OperationResult.succeed({
        "provider": name,
        "providers_file": str(DEFAULT_PROVIDERS_CONFIG),
        "providers": providers,
    })


def llm_provider_delete(cfg: dict[str, Any], provider: str) -> OperationResult:
    """删除供应商连接信息（写回 providers.yaml）。

    若供应商仍被降级链引用，拒绝删除（否则重启 load_llm_config 会因未知供应商抛错）。
    """
    name = (provider or "").strip()
    if not name:
        raise InvalidArgs("供应商名不能为空")
    providers = load_providers_config() or {}
    if name not in providers:
        raise OperationError(f"供应商 {name!r} 不存在", error_code="ERR_NOT_FOUND")
    chains = (cfg.get("llm") or {}).get("chains") or {}
    referencing = [
        cn for cn, nodes in chains.items()
        for nd in (nodes or []) if isinstance(nd, dict) and nd.get("provider") == name
    ]
    if referencing:
        raise InvalidArgs(
            f"供应商 {name!r} 正被降级链 [{', '.join(referencing)}] 引用，请先从链中移除再删除"
        )
    del providers[name]
    write_providers_config(providers)
    return OperationResult.succeed({"provider": name, "deleted": True, "providers": providers})


def _sync_provider_into_chains(cfg: dict[str, Any], name: str, fields: dict[str, Any]) -> None:
    """把供应商最新连接字段注入运行时 cfg 降级链中引用该供应商的节点（幂等）。"""
    chains = (cfg.get("llm") or {}).get("chains") or {}
    for nodes in chains.values():
        for nd in nodes or []:
            if isinstance(nd, dict) and nd.get("provider") == name:
                for k, v in fields.items():
                    if k != "provider" and k not in nd:
                        nd[k] = v


def llm_chain_update(cfg: dict[str, Any], chains: dict[str, list[dict]]) -> OperationResult:
    """整体更新降级链（T-44 降级链编辑：增删节点 + 排序）。

    - 校验每个非 rule 节点引用的供应商必须存在于 providers.yaml（防重启 load 抛错）
    - 白名单校验：命中 deny_prefixes/deny_exact 的模型拒绝写入（铁律 #9）
    - 写回 llm.yaml（保留 rules 等段）并刷新运行时 cfg["llm"]["chains"]
      ——链级参数（rules）+ 连接字段（providers.yaml）不变，仅链结构变化

    Args:
        cfg: 运行时配置（含 llm.rules 与 llm.chains）。
        chains: {chain_name: [节点...]}，节点须含 provider（rule 节点含 rule）。
    """
    if not isinstance(chains, dict):
        raise InvalidArgs("chains 必须是对象")
    providers = load_providers_config() or {}
    for chain_name, nodes in chains.items():
        if not isinstance(nodes, list):
            raise InvalidArgs(f"链 {chain_name!r} 必须是列表")
        for node in nodes:
            if not isinstance(node, dict):
                raise InvalidArgs(f"链 {chain_name!r} 节点必须是对象")
            pname = node.get("provider")
            if not pname:
                raise InvalidArgs(f"链 {chain_name!r} 存在缺 provider 的节点")
            if pname == "rule":
                if not node.get("rule"):
                    raise InvalidArgs(f"链 {chain_name!r} 的 rule 节点缺 rule 动作")
                continue
            if pname not in providers:
                raise InvalidArgs(
                    f"链 {chain_name!r} 引用了未知供应商 {pname!r}（请先在供应商中添加）"
                )
    # 白名单校验（铁律 #9）：rule 节点跳过；rules 取自运行时 cfg（含 llm.yaml 合并的
    # allowed_models 黑名单），否则黑名单校验失效
    from sgme.llm.chain import validate_models
    validate_models({"chains": chains, "rules": (cfg.get("llm") or {}).get("rules") or {}})
    write_llm_config(chains)
    # 刷新运行时 cfg["llm"]["chains"]（连接字段由 load_llm_config 语义注入）
    llm_cfg = cfg.setdefault("llm", {})
    for chain_name, nodes in chains.items():
        for node in nodes:
            if isinstance(node, dict) and node.get("provider") not in (None, "rule"):
                pname = node["provider"]
                if pname in providers:
                    for k, v in providers[pname].items():
                        if k != "name" and k != "vector_capable" and k not in node:
                            node[k] = v
    llm_cfg["chains"] = chains
    return OperationResult.succeed({
        "chains": chains,
        "providers_file": str(DEFAULT_PROVIDERS_CONFIG),
    })


def llm_health(
    cfg: dict[str, Any],
    *,
    probe: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> OperationResult:
    """逐供应商健康探测（只读，robust）。

    Args:
        cfg: 运行时配置。
        probe: 探测函数（可注入，测试用）；默认 ``_probe_provider``。
    """
    chains = (cfg.get("llm") or {}).get("chains") or {}
    providers = _collect_providers(chains)
    # 统一供应商模型（T-47）：探测范围并入 vector_capable 供应商（向量模型也需连通性探测）
    for name, info in _file_providers_with_flags(load_providers_config()).items():
        if name not in providers:
            providers[name] = info
    probe_fn = probe or _probe_provider
    health: dict[str, dict[str, Any]] = {}
    for name, info in providers.items():
        health[name] = probe_fn(info)
    return OperationResult.succeed({"health": health})