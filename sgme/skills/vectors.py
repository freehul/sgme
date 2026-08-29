"""sgme/skills/vectors.py：技能向量缓存与余弦检索（ST-36 M1——可弃缓存文件方案）。

向量存 data/cache/skill_vectors.json（1024 维 × N 条 ≈ 数百 KB），丢了就重建：
启动读缓存 → 内容 SHA 一致直接用 → miss 才调 embedding → 全量后落盘。
embedding 复用 SGME 统一搜索提供商配置（v0.2.1 裁决：bge-m3 / siliconflow）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from sgme.skills.indexer import SkillRecord

logger = logging.getLogger("sgme.skills.vectors")

# 缓存文件格式版本（字段不兼容时整档作废重建）
CACHE_FORMAT = 1


def _cache_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / "skill_vectors.json"


def load_cache(cache_dir: str | Path) -> dict:
    """读取向量缓存；损坏/缺档返回空结构（自愈）。"""
    p = _cache_path(cache_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"format": CACHE_FORMAT, "items": {}}
    if not isinstance(data, dict) or data.get("format") != CACHE_FORMAT:
        return {"format": CACHE_FORMAT, "items": {}}
    items = data.get("items")
    if not isinstance(items, dict):
        items = {}
    return {"format": CACHE_FORMAT, "items": items}


def save_cache(cache_dir: str | Path, cache: dict) -> Path:
    """原子落盘（tmp + os.replace，镜像项目备份/意图文件惯例）。"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _cache_path(cache_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return p


def _embed_config(cfg: dict) -> tuple[str, str, str, str]:
    """从 providers.yaml/search.vector 配置取 embedding 提供商（provider/model/base_url+key）。

    解析顺序（镜像 T-43/T-47 的统一供应商裁决，本地优先 2026-08-20 生产定案）：
    1. search.vector.provider 指名的提供商：在注册表则用注册表连接字段；
       不在注册表（如本地 ollama "local"）则用 search.vector 自带 base_url/model——
       **本地直连优先于云端 vector_capable 扫描**；
    2. 无 active 或 active 不可用 → 扫 providers 中 vector_capable=true 的首个可用者；
    3. 最后兜底：search.vector 自带 base_url 的直连段。
    找不到返回 ("", "", "", "")。

    ⚠️ 2026-08-29 根修（T-118）：load_providers_config() 返回的就是**扁平**
    {name: 连接字段} 字典，原实现误做 .get("providers") 二次解包 → 永远得空表
    → 技能向量嵌入从未真正工作过（M1 起潜伏，被 mock 形状错误的测试掩护）。
    """
    prov_name = model = ""
    base_url = api_key = ""
    try:
        from sgme.config import load_providers_config

        providers = load_providers_config() or {}
    except Exception:
        providers = {}
    import os

    def _key_value(env_name: object) -> str:
        env_name = str(env_name or "").strip()
        return os.environ.get(env_name, "") if env_name else ""

    search_cfg = ((cfg or {}).get("search") or {}).get("vector") or {}
    active = str(search_cfg.get("provider") or "").strip()
    if active:
        p = providers.get(active)
        if isinstance(p, dict) and (p.get("api_key_env") or p.get("base_url")):
            # 活跃提供商在注册表：用注册表连接字段（default_model 缺省回落 search.vector.model）
            return (
                active,
                str(p.get("default_model") or search_cfg.get("model") or ""),
                str(p.get("base_url") or ""),
                _key_value(p.get("api_key_env")),
            )
        if search_cfg.get("base_url"):
            # 活跃提供商不在注册表（本地 ollama 场景）：直接用 search.vector 自带连接字段
            return (
                active,
                str(search_cfg.get("model") or ""),
                str(search_cfg.get("base_url") or ""),
                _key_value(search_cfg.get("api_key_env")),
            )
    # 无 active / active 不可用：扫 vector_capable=true 的提供商（保持配置顺序）
    for name, p in providers.items():
        if (
            isinstance(p, dict)
            and p.get("vector_capable")
            and (p.get("api_key_env") or p.get("base_url"))
        ):
            return (
                name,
                str(p.get("default_model") or search_cfg.get("model") or ""),
                str(p.get("base_url") or ""),
                _key_value(p.get("api_key_env")),
            )
    # 最后兜底：search.vector 自带 base_url 的直连段（无 provider 名也允许）
    if search_cfg.get("base_url"):
        return (
            "",
            str(search_cfg.get("model") or ""),
            str(search_cfg.get("base_url") or ""),
            _key_value(search_cfg.get("api_key_env")),
        )
    return prov_name, model, base_url, api_key


def _embed_batch(texts: list[str], cfg: dict, timeout: float) -> dict[str, list[float]]:
    """单批调 embedding API（OpenAI 兼容 /embeddings）；失败抛异常由调用方决定降级。"""
    provider, model, base_url, api_key = _embed_config(cfg)
    if not (provider and model and base_url):
        raise RuntimeError("向量模型未配置（providers 无 vector_capable 且 search.vector 无兜底）")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.post(
        base_url.rstrip("/") + "/embeddings",
        json={"model": model, "input": texts},
        headers=headers,
        timeout=timeout,
        trust_env=False,  # 项目铁律：防代理劫持
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return {str(d["index"]): d["embedding"] for d in data}


def _embed_batch_params(cfg: dict) -> tuple[int, float]:
    """取分批大小与单批超时（可由 skills 配置覆盖）。

    默认值来自 2026-08-29 容器实测（Ollama bge-m3，每条约 500 字符）：
    ``1 条 2.1s / 10 条 19.2s / 50 条 35s 超时`` —— 故批大小取 10、超时放宽到 60s。
    """
    raw = (cfg.get("skills") or {}) if isinstance(cfg, dict) else {}
    try:
        size = int(raw.get("embed_batch_size") or 10)
    except (TypeError, ValueError):
        size = 10
    try:
        timeout = float(raw.get("embed_timeout") or 60.0)
    except (TypeError, ValueError):
        timeout = 60.0
    return max(1, size), max(5.0, timeout)


def embed_texts(texts: list[str], cfg: dict) -> dict[str, list[float]]:
    """批量 embed（**内部分批**），返回 {原索引字符串: 向量}。

    ⚠️ 2026-08-29 修复（T-112）：原实现把全部 texts 一次性 POST，
    403 条技能必然撞 timeout（30s）→ 异常被 build_vectors 吞掉 →
    缓存永不落盘 → **向量路永久降级 BM25**（死循环）。
    改成分批后单批 ≤10 条，失败批跳过不影响其他批（部分成功即可用）。

    Raises:
        RuntimeError: 所有批次都失败时抛出（调用方按降级处理）。
    """
    if not texts:
        return {}
    size, timeout = _embed_batch_params(cfg)
    result: dict[str, list[float]] = {}
    failed = 0
    for start in range(0, len(texts), size):
        chunk = texts[start : start + size]
        try:
            part = _embed_batch(chunk, cfg, timeout)
        except Exception as e:
            failed += 1
            logger.warning(
                "技能向量嵌入第 %d-%d 条失败（跳过该批，其余照常）: %s",
                start, start + len(chunk) - 1, e,
            )
            continue
        for j, vec in part.items():
            try:
                result[str(start + int(j))] = vec
            except (TypeError, ValueError):
                continue
    if not result and failed:
        raise RuntimeError(f"技能向量嵌入全部 {failed} 批失败（embedding 不可达）")
    return result


def build_vectors(records, cfg: dict, cache_dir: str | Path,
                  policy: str = "lazy",
                  max_new: int | None = None) -> dict[str, list[float]]:
    """按需补齐记录向量并落盘；返回 {name: vec}。

    policy=lazy（默认）：SHA 命中缓存的直接复用，只嵌 miss 部分；
    policy=refresh：全量重嵌（测试/强制刷新用）。
    embedding 失败：已命中部分照常可用，miss 部分跳过（WARNING，检索降级 BM25 单路）。

    Args:
        max_new: 本次调用最多新嵌入多少条（None=不限）。请求路径应给小值
            （如 10）避免首轮全量把请求拖死，剩余由后台预热逐轮补齐。
    """
    cache = load_cache(cache_dir)
    items = cache["items"]
    need: list[SkillRecord] = []
    result: dict[str, list[float]] = {}
    for rec in records:
        hit = items.get(rec.sha256)
        if isinstance(hit, list) and hit and policy != "refresh":
            result[rec.name] = hit
        else:
            need.append(rec)
    # ⚠️ 单次调用上限（T-112）：首次全量 403 条 × 分批 ≈ 13 分钟，
    # 若放在请求线程内同步跑会把请求拖死。请求路径只补一小批，
    # 剩余交给后台预热任务逐轮补齐（向量逐步可用，不阻塞检索）。
    if max_new is not None and max_new > 0 and len(need) > max_new:
        logger.info(
            "技能向量待补 %d 条，本次只处理 %d 条（余下交后台预热）", len(need), max_new,
        )
        need = need[:max_new]
    if need:
        try:
            embs = embed_texts([r.content[:2000] for r in need], cfg)
            for i, rec in enumerate(need):
                vec = embs.get(str(i))
                if vec:
                    result[rec.name] = vec
                    items[rec.sha256] = vec
            save_cache(cache_dir, cache)
        except Exception as e:  # 容错：向量路降级不影响 BM25 主路
            logger.warning("skills 向量嵌入失败（BM25 单路降级）: %s", e)
    return result


def cosine_topk(query_vec: list[float], vectors: dict[str, list[float]],
                top_k: int = 10) -> dict[str, float]:
    """余弦相似度 top-k（纯 Python，百条规模微秒级，无 numpy 依赖）。"""
    def _norm(v):
        return sum(x * x for x in v) ** 0.5 or 1.0

    qn = _norm(query_vec)
    out: dict[str, float] = {}
    for name, v in vectors.items():
        if len(v) != len(query_vec):
            continue  # 维度不符（换模型残留）：跳过待重嵌
        dot = sum(a * b for a, b in zip(query_vec, v))
        out[name] = dot / (qn * _norm(v))
    ranked = sorted(out.items(), key=lambda kv: -kv[1])[:top_k]
    return dict(ranked)
