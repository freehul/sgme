"""operations/prompts.py：提示词版本管理操作（0.8 T-8，#33）。

职责：`/v1/admin/prompts` 五端点的业务编排，形状照 ``operations/health.py`` 样板
三段式：常量/私有工具 → ``xxx(...) -> OperationResult`` 操作函数 →
``http_payload_*`` 投影函数。**不认识任何协议**（不 import fastapi，不知道 HTTP
状态码），错误码 → 状态码由入口层 ``server/app.py::ERROR_CODES`` 映射。

五个操作对应契约 §6（提示词版本管理）：

- ``prompts_list()``                → GET  /v1/admin/prompts
- ``prompts_publish(stage, note)``  → POST /v1/admin/prompts/publish
- ``prompts_activate(stage, ref)``  → POST /v1/admin/prompts/activate
- ``prompts_ab(stage, a, b, ...)``  → POST /v1/admin/prompts/ab
- ``prompts_metrics(conn, stage)``  → GET  /v1/admin/prompts/metrics

业务语义与改造前 ``server/routes_prompts.py`` 逐行等价（响应格式不变是硬约束）：

1. **错误翻译**：``PromptManifestError``（未知 stage / 缺占位符 / 版本文件不存在 /
   A/B 配置非法）→ ``InvalidArgs`` → 入口层映射 HTTP 400 ERR_INVALID_ARGS，
   与旧 ``_err()`` 的 ``api_error("ERR_INVALID_ARGS", str(e))`` 同码同文案。
2. **列表容错**：单 stage 配置读取失败（坏 manifest 段）不拖垮整个列表——
   回落 ``{"active": "@working", "ab": {"enabled": False}}`` 并记 warning（历史行为保留）。
3. **A/B 关闭分支**：``enabled=false`` 时 a/b/split/bucket_by 全部忽略，
   只写 ``ab.enabled=false``（回落到 active 指向），split 按历史行为传 0.5 占位。
4. **观测不动手**：metrics 只汇总原始观测（runs/error_runs/memories/avg_priority/
   action_dist），不做自动裁决（红线 §6 #1；结论留人工 + 评测集 #32）。

投影说明：prompts 链路只有 HTTP 一个消费方（MCP 无 prompts 工具），操作返回的
``data`` 即历史契约形态（超集 == 契约），``http_payload_*`` 为显式恒等投影——
保留三段式形状，未来若出现第二个消费方或响应加字段，在投影处裁剪即可，
路由与操作互不影响。

依赖方向：只调 ``sgme.prompts``（版本管理器）与 ``sgme.data.refine_dao``
（观测汇总），两者均在 operations 之下的业务层。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict
from typing import Any

from sgme.data.refine_dao import RefineRunRecorder
from sgme.operations.errors import InvalidArgs, OperationResult
from sgme.prompts import PromptManifestError, PromptStore
from sgme.prompts.manager import STAGES

logger = logging.getLogger("sgme.operations.prompts")


# ---------- 常量 ----------

# 受管 stage 清单：与 sgme/prompts/manager.py::STAGES 同一权威源，
# 不在本模块重复抄录（旧 routes_prompts 自抄一份正是抽取要消灭的重复）。


# ---------- 私有工具 ----------

def _store(store: PromptStore | None = None) -> PromptStore:
    """取 PromptStore 实例：显式注入优先，否则默认构造。

    默认构造读取类属性 ``PROMPTS_ROOT`` / ``MANIFEST_PATH``（生产指向项目
    ``prompts/``；测试 monkeypatch 类属性即隔离）。``store`` 参数供
    测试/复用方注入指向临时目录的实例。
    """
    return store if store is not None else PromptStore()


def _manifest_error_as_invalid_args(e: PromptManifestError) -> InvalidArgs:
    """PromptManifestError → InvalidArgs（入口层映射 400 ERR_INVALID_ARGS）。

    文案透传 ``str(e)``，与改造前 ``_err()`` 的 api_error message 逐字一致。
    """
    return InvalidArgs(str(e))


# ---------- 操作：GET /v1/admin/prompts ----------

def prompts_list(store: PromptStore | None = None) -> OperationResult:
    """列出全部 stage 的 active / ab / versions（契约 §6 列表端点业务）。

    纯只读、幂等。逐 stage 组装：active 指向（默认 @working）、ab 配置
    （未配置时 ``{"enabled": False}``）、已发布版本元数据（按 version 升序）。

    Args:
        store: PromptStore 实例；None 时默认构造（读类属性路径）。

    Returns:
        OperationResult(ok=True)，data 形如
        ``{"stages": [{"stage", "active", "ab", "versions": [...]}, ...]}``。
        每条 versions 元素含 version / file / sha256 / created_at / note
        （与 manifest versions 段一致）。

    Raises:
        InvalidArgs: 本操作不抛——单 stage 配置损坏按历史行为回落默认并告警。
    """
    s = _store(store)
    out: dict[str, Any] = {"stages": []}
    for stage in STAGES:
        versions = [asdict(v) for v in s.list_versions(stage)]
        try:
            scfg = s.stage_config(stage)
        except PromptManifestError as e:
            # 单 stage 配置损坏不拖垮整个列表：回落 @working 默认并告警（历史行为保留）
            scfg = {"active": "@working", "ab": {"enabled": False}}
            logger.warning("提示词列表读取 stage=%s 配置失败: %s", stage, e)
        out["stages"].append({
            "stage": stage,
            "active": scfg.get("active", "@working"),
            "ab": scfg.get("ab") or {"enabled": False},
            "versions": versions,
        })
    return OperationResult.succeed(out)


# ---------- 操作：POST /v1/admin/prompts/publish ----------

def prompts_publish(
    stage: str,
    note: str = "",
    store: PromptStore | None = None,
) -> OperationResult:
    """发布新版本：工作副本 → versions/<stage>/vNNN.txt（原子写，#33）。

    前置校验（PromptStore.publish 内）：stage 已知、工作副本存在、必备占位符完整；
    失败一律 → InvalidArgs（400）。

    Args:
        stage: stage 名（tier0_summary / l1_extraction / l1_conflict / l2_scene）。
        note: 发布说明（≤200 字符，入口层 pydantic 已限长）。
        store: PromptStore 实例；None 时默认构造。

    Returns:
        OperationResult(ok=True)，data 形如
        ``{"status": "ok", "version": {version, file, sha256, created_at, note}}``。

    Raises:
        InvalidArgs: 未知 stage / 工作副本缺失 / 缺必备占位符。
    """
    s = _store(store)
    try:
        info = s.publish(stage, note=note)
    except PromptManifestError as e:
        raise _manifest_error_as_invalid_args(e) from e
    logger.info("admin 发布提示词: stage=%s version=%s", stage, info.version)
    return OperationResult.succeed({"status": "ok", "version": asdict(info)})


# ---------- 操作：POST /v1/admin/prompts/activate ----------

def prompts_activate(
    stage: str,
    version_ref: str,
    store: PromptStore | None = None,
) -> OperationResult:
    """激活版本：@working（热更新）或钉版 vNNN / versions/<stage>/vNNN.txt。

    Args:
        stage: stage 名。
        version_ref: 版本引用（@working / vNNN / versions/<stage>/vNNN.txt）。
        store: PromptStore 实例；None 时默认构造。

    Returns:
        OperationResult(ok=True)，data 形如
        ``{"status": "ok", "stage": stage, "active": version_ref}``。

    Raises:
        InvalidArgs: 未知 stage / 版本文件不存在。
    """
    s = _store(store)
    try:
        s.activate(stage, version_ref)
    except PromptManifestError as e:
        raise _manifest_error_as_invalid_args(e) from e
    logger.info("admin 激活提示词: stage=%s active=%s", stage, version_ref)
    return OperationResult.succeed({"status": "ok", "stage": stage, "active": version_ref})


# ---------- 操作：POST /v1/admin/prompts/ab ----------

def prompts_ab(
    stage: str,
    a: str | None = None,
    b: str | None = None,
    split: float = 0.5,
    bucket_by: str = "file_id",
    enabled: bool = True,
    store: PromptStore | None = None,
) -> OperationResult:
    """配置 A/B 分流（enabled=false 关闭，下次渲染起回落 active 指向）。

    参数校验分两层：enabled=true 时 a/b 必填（本操作校验，→ InvalidArgs）；
    split 越界 / bucket_by 非法 / a/b 文件不存在 / a==b 由 PromptStore 校验
    （PromptManifestError → InvalidArgs）。split 的 [0,1] 范围同时由入口层
    pydantic 拦截（422），两处校验与改造前一致。

    Args:
        stage: stage 名。
        a / b: A/B 版本引用；enabled=true 时必填。
        split: A 流量占比 [0,1]。
        bucket_by: file_id | memory_id | random。
        enabled: false 时关闭 A/B（忽略 a/b/split/bucket_by）。
        store: PromptStore 实例；None 时默认构造。

    Returns:
        OperationResult(ok=True)。data 形如
        enabled=true  → ``{"status": "ok", "stage", "ab_enabled": True, "a", "b", "split", "bucket_by"}``
        enabled=false → ``{"status": "ok", "stage", "ab_enabled": False}``
        （与改造前响应逐字段一致：关闭时不回显 a/b/split/bucket_by）。

    Raises:
        InvalidArgs: enabled=true 且 a/b 缺失；或 PromptStore 语义校验失败。
    """
    s = _store(store)
    try:
        if enabled:
            if not a or not b:
                raise InvalidArgs("enabled=true 时 a/b 必填")
            s.configure_ab(stage, a, b, split, bucket_by=bucket_by, enabled=True)
        else:
            # 关闭分支：忽略 a/b/split/bucket_by，只写 enabled=false（历史行为）
            s.configure_ab(stage, "", "", 0.5, enabled=False)
    except PromptManifestError as e:
        raise _manifest_error_as_invalid_args(e) from e
    logger.info("admin 配置 A/B: stage=%s enabled=%s split=%s", stage, enabled, split)
    data: dict[str, Any] = {"status": "ok", "stage": stage, "ab_enabled": enabled}
    if enabled:
        data.update({"a": a, "b": b, "split": split, "bucket_by": bucket_by})
    return OperationResult.succeed(data)


# ---------- 操作：GET /v1/admin/prompts/metrics ----------

def prompts_metrics(
    mem_conn: sqlite3.Connection,
    stage: str,
    since: str | None = None,
) -> OperationResult:
    """A/B 观测汇总：按 (version, variant) 分组 runs / error / memories / avg(priority) / action 分布。

    只做观测，不做自动裁决（红线 §6 #1；结论留人工 + 评测集 #32）。
    数据来自 ``RefineRunRecorder.summarize``（data 层唯一统计出口）。

    Args:
        mem_conn: memory.db 连接（refine_runs / memories 所在库）。
        stage: stage 名；未知 stage → InvalidArgs（400，与改造前同文案）。
        since: 仅统计 started_at >= since 的 run（可选）。

    Returns:
        OperationResult(ok=True)，data 形如
        ``{"stage", "since", "groups": [{"version", "variant", "runs",
        "error_runs", "memories_count", "memories_rows", "avg_priority",
        "action_dist"}, ...]}``。

    Raises:
        InvalidArgs: 未知 stage。
    """
    if stage not in STAGES:
        raise InvalidArgs(f"未知 stage: {stage}（合法: {list(STAGES)}）")
    return OperationResult.succeed(RefineRunRecorder.summarize(mem_conn, stage, since=since))


# ---------- HTTP 投影 ----------

# prompts 链路只有 HTTP 一个消费方（MCP 无 prompts 工具），操作返回的 data
# 即历史契约形态（超集 == 契约），以下投影为显式恒等：保持三段式形状，
# 若未来出现第二个消费方或响应加字段，在投影处裁剪即可，路由与操作互不影响。

def http_payload_list(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 GET /v1/admin/prompts 历史契约形态（改造前逐字段等价）。"""
    return data


def http_payload_publish(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 POST /v1/admin/prompts/publish 历史契约形态（改造前逐字段等价）。"""
    return data


def http_payload_activate(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 POST /v1/admin/prompts/activate 历史契约形态（改造前逐字段等价）。"""
    return data


def http_payload_ab(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 POST /v1/admin/prompts/ab 历史契约形态（改造前逐字段等价）。"""
    return data


def http_payload_metrics(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 GET /v1/admin/prompts/metrics 历史契约形态（改造前逐字段等价）。"""
    return data
