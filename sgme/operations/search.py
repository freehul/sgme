"""operations/search.py：混合检索操作（v0.7 §7 operations 层样板模块）。

承接入口
--------
- HTTP ``POST /v1/search``（routes_memory.search_memories，Agent Key）
- MCP ``search`` 工具（mcp_server.search）

三段式结构（照抄 health.py 样板）：
1. 常量/私有工具（本模块内聚，不外泄）
2. ``search(...) -> OperationResult`` 操作函数：显式接参，返回**协议无关的信息超集**
3. ``http_payload(data)`` / ``mcp_payload(data)`` 投影函数：把超集裁剪成各入口的历史契约形态

请求字段（v0.6 SearchRequest，HTTP 与 MCP 的并集）
--------------------------------------------------
- query: 检索词（HTTP 必填 str；MCP 必填 str）。**空串不报错**——
  v0.6 行为是返回空结果（tests/test_server.py::test_search_empty_query_returns_empty）
- scopes: 检索层列表，HTTP 缺省 ["memory"]；MCP 固定 memory-only（无 scopes 参数）
  - "memory" → 记忆池（FTS5 BM25 + 维度标签过滤 + 向量 + RRF + 溯源 trace）
  - "wiki" / "scenes" → wiki 场景叙事文档（L2，FTS + LIKE 兜底 + 预留向量路）
  - "wiki_pages" → wiki 知识库页面（wiki_pages 表，T-34 新增；FTS5 BM25 + LIKE 兜底，
    wiki_conn 为 None / 检索失败 → 该层空结果，不影响其他层）
  - 未知 scope 值被**忽略**（v0.6 行为，不报错；
    tests/test_server.py::test_search_no_memory_scope_returns_empty）
- dimensions: 维度标签过滤（可选；match=any 至少命中一个 / match=all 全部命中）
- match: 维度匹配语义，缺省 "any"
- limit: 每层结果上限（HTTP 缺省 10；MCP 缺省 5，入口侧再 ``min(limit, 20)`` 封顶）
- include_sources: 是否展开溯源 trace（缺省 True）

响应结构（历史契约差异，v0.8 待统一，现在**不得**合并）
------------------------------------------------------
- HTTP：``{"results": [...], "meta": {"routes": [...], "rrf_k": 60}}``
  - ``results``：先 memory 层后 wiki 层，按 scopes 顺序拼接
  - ``meta.routes``：各结果 ``routes`` 的去重并集（保序），无命中时回退 ``["bm25"]``
- MCP：``{"results": [...]}`` —— **没有 meta**，且只查 memory 层

⚠️ ``meta.rrf_k`` 是 v0.6 路由里的**历史硬编码 60**（不读 cfg ``search.rrf.k``），
本模块以常量 ``META_RRF_K`` 保留该行为，保证响应逐字节等价；
统一为 cfg 取值属 v0.8 议题，届时删除本常量即可。

异常翻译（与入口层 run_operation / _op_json 配合）
--------------------------------------------------
- 参数校验不通过（query 非字符串 / scopes 非字符串列表）→ 抛 ``InvalidArgs``
  → ``ERR_INVALID_ARGS``（HTTP 400 / MCP error JSON），镜像 v0.6 pydantic 校验语义
- 检索内部异常（sqlite 故障、检索函数抛错）→ ``OperationResult.fail(ERR_INTERNAL)``
  → HTTP 500 / MCP error JSON。v0.6 时这类异常由全局异常处理器兜底为 500，
  本模块统一收敛为 ``ERR_INTERNAL`` 错误码（状态码不变，错误形态归一）

依赖：调 ``sgme.data.search``（search_memories / search_scenes）+ ``sgme.wiki.fts``
（search_wiki_fts，扩展模块检索，同 operations/wiki.py 先例），engine 是禁区。
副作用：无（检索为纯读路径；向量不可达时由业务层自动降级 BM25/LIKE）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

import httpx

from sgme.data import search as search_mod
from sgme.operations.errors import ERR_INTERNAL, InvalidArgs, OperationResult
from sgme.wiki import fts as wiki_fts

logger = logging.getLogger("sgme.operations.search")

# HTTP meta.rrf_k 的历史硬编码值：v0.6 路由写死 60（不读 cfg search.rrf.k），
# 为逐字节等价保留；v0.8 统一为 cfg 取值后删除本常量。
META_RRF_K: int = 60

# scopes 缺省值：与 v0.6 SearchRequest 的 pydantic 缺省一致（HTTP 侧）。
DEFAULT_SCOPES: list[str] = ["memory"]

# 词边界守卫（ASCII 字母数字）：防别名替换误伤派生词（daemons 不触发 daemon）
_WORD_BOUNDARY = r"(?<![A-Za-z0-9]){alias}(?![A-Za-z0-9])"


def normalize_query_terms(query: str, term_aliases: dict[str, str]) -> str:
    """检索术语别名归一化（ST-19）：旧术语 → 标准术语，大小写/空格容忍。

    实现为**查询扩展**而非纯替换：命中别名的词保留原文并追加标准术语
    （如 ``daemon`` → ``daemon gateway``）。理由：纯替换会让「旧术语老记忆」
    从此无法召回（回归），扩展则新老记忆双向可召回——旧术语查询命中新名记忆，
    标准术语查询同样命中旧名记忆，且不含别名的查询逐字符不变。

    规则：
    - 词边界整体匹配：daemons / daemonize 等派生词不触发扩展
    - 大小写容忍：别名匹配忽略大小写；注入的标准术语统一小写
      （FTS5/LIKE 对 ASCII 大小写不敏感，等价匹配）
    - 空格容忍：查询与别名内部连续空白折叠为单空格（SGME  Server 等价 SGME Server）
    - 标准术语已在查询中 → 跳过注入（防重复）；同一标准术语只注入一次
    - 不命中时行为不变：query 不含任何别名 → 原样返回（连大小写/空白都不动）

    Args:
        query: 原始检索词（可为空串）。
        term_aliases: {旧术语: 标准术语} 映射（cfg['term_aliases']，来自
            registry/term_aliases.yaml；为空/None 等价于不做归一化）。

    Returns:
        扩展后的检索词；无别名命中时返回原 query（逐字符不变）。
    """
    if not query or not term_aliases:
        return query
    out = _collapse_spaces(query)
    changed = False
    injected: set[str] = set()
    for alias, canonical in term_aliases.items():
        alias_key = _collapse_spaces(str(alias)).lower()
        if not alias_key:
            continue
        canonical_key = _collapse_spaces(str(canonical)).lower()
        if not canonical_key or canonical_key in injected:
            continue
        # 标准术语已在查询中 → 无需注入（防重复）
        if re.search(_WORD_BOUNDARY.format(alias=re.escape(canonical_key)), out, re.IGNORECASE):
            continue
        pattern = re.compile(
            _WORD_BOUNDARY.format(alias=re.escape(alias_key)), re.IGNORECASE
        )

        def _inject(match: re.Match, _canonical: str = canonical_key) -> str:
            nonlocal changed
            changed = True
            return f"{match.group(0)} {_canonical}"

        # 仅当别名实际命中才注入并记账（防同一标准术语重复注入，
        # 同时不阻塞「本别名未命中、其他别名仍需注入」的路径）
        if pattern.search(out):
            out = pattern.sub(_inject, out)
            injected.add(canonical_key)
    # 无别名命中 → 返回原始 query（不命中时行为不变，逐字符等价）
    return out if changed else query


def _collapse_spaces(text: str) -> str:
    """连续空白折叠为单空格（大小写/空格容忍的归一化前置）。"""
    return re.sub(r"\s+", " ", text).strip()


def _search_wiki_pages(
    wiki_conn: sqlite3.Connection | None,
    query: str,
    limit: int,
) -> list[dict]:
    """wiki 知识库页面检索层（T-34，scope="wiki_pages"）。

    - 复用扩展模块 ``sgme.wiki.fts.search_wiki_fts``（FTS5 BM25 + LIKE 兜底，
      同 operations/wiki.py 先例），不重复造轮子；
    - **容错隔离**：wiki_conn 为 None（wiki 扩展未挂载/入口未注入）或检索
      抛异常 → 返回空列表 + WARNING，不拖累 memory / scenes 层；
    - 结果形状对齐 scenes 层（rank/source/title/content/routes），
      source 标记 ``"wiki_pages"``，routes 标记 ``["wiki_fts"]``。
    """
    if wiki_conn is None:
        return []
    try:
        rows = wiki_fts.search_wiki_fts(wiki_conn, query, limit=limit)
    except Exception as e:
        logger.warning("wiki_pages 检索失败（该层空结果）: %s", e)
        return []
    return [
        {
            "rank": i + 1,
            "source": "wiki_pages",
            "page_id": r["page_id"],
            "title": r["title"],
            "content": r.get("snippet") or "",
            "routes": ["wiki_fts"],
        }
        for i, r in enumerate(rows)
    ]


def search(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    query: str,
    scopes: list[str] | None = None,
    dimensions: list[str] | None = None,
    match: str = "any",
    limit: int = 10,
    include_sources: bool = True,
    client: httpx.Client | None = None,
    wiki_conn: sqlite3.Connection | None = None,
) -> OperationResult:
    """混合检索：记忆池（BM25+向量+RRF）+ wiki 场景（L2）+ wiki 知识库页面。

    签名刻意**只收业务依赖**（连接 + cfg + 检索参数），operations 层不认识
    ``request.app.state`` 或 mcp 的 ``_app_state``（那是入口层的协议细节），
    由入口层取出后显式传入——照抄 health.py 样板。

    Args:
        mem_conn: memory.db 连接（v0.7 三库拆分后 scenes 系列同在 memory.db，
            故记忆池与 wiki 场景共用本连接）。
        session_conn: session.db 连接（溯源 trace 查 raw_files 用）。
        cfg: 运行时配置（向量检索开关 / RRF k 等，透传业务层）。
        query: 检索词。空串**不报错**（v0.6 行为：返回空结果）。
        scopes: 检索层列表；None → ["memory"]（pydantic 缺省语义）。
            未知 scope 值被忽略（v0.6 行为），不报错。
        dimensions: 维度标签过滤（可选）。
        match: "any"=命中任一维度 / "all"=全部命中，缺省 any。
        limit: 每层结果上限。
        include_sources: 是否展开溯源 trace。
        client: 可选 httpx 客户端（向量 embed 注入点；缺省 None = v0.6 行为）。
        wiki_conn: wiki.db 连接（wiki_pages 层用）。None 或该层检索失败 →
            空结果，不影响 memory / scenes 层（T-34：wiki 扩展不可用不拖累整体）。

    Returns:
        OperationResult(ok=True)，data 为协议无关信息超集：
        - results: 合并结果列表（memory → wiki 场景 → wiki_pages，按 scopes 顺序）
        - routes: 各结果 routes 的去重并集（保序；可能为空列表）
        - rrf_k: HTTP meta 用的 RRF k（历史硬编码 60，见模块 docstring）

    Raises:
        InvalidArgs: query 非字符串 / scopes 非字符串列表（→ ERR_INVALID_ARGS）。
    """
    # —— 参数校验（镜像 v0.6 pydantic 校验语义；空 query 除外：v0.6 返回空结果） ——
    if not isinstance(query, str):
        raise InvalidArgs("query 必须为字符串")
    if scopes is None:
        scopes = DEFAULT_SCOPES
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise InvalidArgs("scopes 必须为字符串列表")

    # —— 检索术语别名归一化（ST-19）：旧术语查询 → 标准术语后检索 ——
    # 统一在 operations 层（HTTP/MCP 唯一查询入口）做，data 层保持纯检索；
    # 归一化后 FTS5/LIKE/向量三条路共用同一查询串，旧术语自动命中新名记忆。
    query = normalize_query_terms(query, cfg.get("term_aliases") or {})

    results: list[dict] = []
    try:
        # 层 1：memory 记忆池（BM25 + 向量 + RRF，v0.6 路由逐行等价）
        if "memory" in scopes:
            results.extend(
                search_mod.search_memories(
                    mem_conn, session_conn,
                    query=query,
                    dimensions=dimensions,
                    match=match,
                    limit=limit,
                    include_sources=include_sources,
                    cfg=cfg,
                    client=client,
                )
            )
        # 层 2：wiki 场景叙事文档（L2；"scenes" 为历史别名 scope，两者等价）
        if "wiki" in scopes or "scenes" in scopes:
            results.extend(
                search_mod.search_scenes(
                    mem_conn, query, limit=limit, cfg=cfg, client=client,
                )
            )
        # 层 3：wiki 知识库页面（wiki_pages 表，T-34 新增）
        # 容错隔离：wiki_conn 为 None 或检索失败 → 该层空结果 + WARNING，
        # 不拖累 memory / scenes 层（wiki 扩展不可用≠整体搜索失败）。
        if "wiki_pages" in scopes:
            results.extend(_search_wiki_pages(wiki_conn, query, limit))
    except Exception as e:
        # 检索内部错误 → ERR_INTERNAL（v0.6 由全局异常处理器兜底为 500，
        # 状态码不变；错误码统一收敛到 operations 层）
        return OperationResult.fail(ERR_INTERNAL, f"检索失败: {e}")

    # 聚合实际命中的 routes（取并集、保序——v0.6 路由逐行等价）
    routes_seen: list[str] = []
    for r in results:
        for rt in r.get("routes", []):
            if rt not in routes_seen:
                routes_seen.append(rt)

    return OperationResult.succeed(
        {
            "results": results,
            "routes": routes_seen,
            "rrf_k": META_RRF_K,
        }
    )


def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP ``POST /v1/search`` 的历史契约形态（v0.6 逐字段等价）。

    ``meta.routes`` 无命中时回退 ``["bm25"]``（v0.6 路由 ``routes_seen or ["bm25"]``）。
    """
    return {
        "results": data["results"],
        "meta": {
            "routes": data["routes"] or ["bm25"],
            "rrf_k": data["rrf_k"],
        },
    }


def mcp_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 MCP ``search`` 工具的历史契约形态（v0.6 逐字段等价）。

    与 http_payload 的差异：**无 meta**，且只查 memory 层（scope 由入口侧传
    ``["memory"]``）——历史差异，v0.8 待统一。

    不做 ``results[:limit]`` 二次切片：v0.6 的该切片因检索上限恒 ≤ limit
    恒为无操作（``do_search(limit=min(limit,20))`` 返回 ≤ min(limit,20) 条，
    恒 ≤ limit），等价保留。
    """
    return {"results": data["results"]}
