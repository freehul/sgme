"""operations/config.py：配置读写操作（v0.7 §7）。

⚠️ 命名雷区（务必先读）
------------------------
本文件是 ``sgme/operations/config.py``，与项目既有的 ``sgme/config.py``
是**两个不同模块**。二者在本文件里同时出现，故：

- 本模块内一律 ``from sgme import config as sgme_config``，
  用 ``sgme_config.xxx`` 访问配置层，绝不裸写 ``config.xxx``；
- 调用方一律走完整路径 ``from sgme.operations.config import <op>``，
  **禁止** ``from sgme.operations import config``——那样 ``config`` 会与
  ``from sgme import config`` 在同一命名空间里撞名（``operations/__init__.py``
  「导入规范」理由 2 即为此）。

三段式结构（照抄 health.py 样板）：
1. 私有工具（本模块内聚）
2. ``xxx(...) -> OperationResult`` 操作函数：返回协议无关的信息超集
3. ``*_payload(...)`` 投影函数：把超集裁剪成各入口的历史契约形态

承接的入口
----------
=============================================  ===================================
入口                                            操作
=============================================  ===================================
HTTP ``GET /v1/admin/config``                   ``get_config`` + ``get_http_payload``
HTTP ``GET /v1/admin/config/{section}``         ``get_config_section`` + ``get_http_payload``
HTTP ``PUT|POST /v1/admin/config``              ``update_config`` + ``update_payload``
MCP  ``config_get``                             ``get_config`` + ``get_mcp_payload``
MCP  ``config_update``                          ``update_config_section`` + ``update_payload``
=============================================  ===================================

⚠️ 历史契约差异（v0.8 待统一，现在**不得**合并）
--------------------------------------------------
读路径：

- 未知段校验强度不同。HTTP ``/{section}`` 未知段 → **404**；
  MCP ``config_get("不存在的段")`` → **不校验**，直接回 ``{"section": s, "config": null}``。
  这不是形态差异而是**行为差异**，投影函数救不了，故拆成两个操作函数
  （``get_config`` 宽松版 / ``get_config_section`` 严格版），共用同一实现内核。
- 全量读的字段集不同：HTTP 多一个 ``writable_sections``，MCP 没有。
  这个由 ``get_http_payload`` / ``get_mcp_payload`` 两个投影还原。

写路径：

- 未知段文案不同。HTTP 单段：``未知配置段: {s}（可用: [...]）``；
  HTTP 多段 与 MCP：``未知配置段: {s}``（无列表）。同一语义三处两版文案。
- 落盘失败处理不同。HTTP **不捕获**（异常上抛 → 全局处理器 → 500
  ``内部错误: {e}``）；MCP **捕获**并回 ``{"error": "配置落盘失败: {e}"}``。
- 请求形态不同。HTTP 支持 ``section=None`` 的「多段一次改」形态；MCP 只有单段。

同样是行为差异而非形态差异，故写路径也拆成两个操作函数
（``update_config`` HTTP 版 / ``update_config_section`` MCP 版），
共用私有内核 ``_apply_values``；响应形态则由同一个 ``update_payload`` 投影。

依赖：只调 ``sgme.config``（AGENTS.md 约束：config 是配置唯一读写方，含落盘）。
"""
from __future__ import annotations

from typing import Any

from sgme import config as sgme_config
from sgme.operations.errors import (
    ERR_INTERNAL,
    ERR_INVALID_ARGS,
    ERR_NOT_FOUND,
    OperationResult,
)


def _all_sections(cfg: dict[str, Any]) -> dict[str, Any]:
    """全量段快照 ``{段名: 段内容}``。

    ⚠️ 刻意按 ``CONFIG_SECTIONS``（set）的**原生迭代序**构造，而非 ``sorted``——
    v0.6 两端都写的是 ``{s: cfg.get(s) for s in CONFIG_SECTIONS}``，
    改成排序会改变 JSON 键序，破坏「响应逐字节等价」。
    """
    return {s: cfg.get(s) for s in sgme_config.CONFIG_SECTIONS}


def _snapshot(cfg: dict[str, Any], section: str | None) -> dict[str, Any]:
    """构造读/写操作共用的信息超集。

    Args:
        cfg: 运行时配置字典。
        section: 目标段名；None 表示「整体」形态。

    Returns:
        - section: 回显的段名（None 表示整体形态，投影据此选分支）
        - section_config: 该段内容（section 为 None 时恒为 None）
        - config: 全量段快照
        - writable_sections: 可写段白名单（已排序，HTTP 全量读专用）
    """
    return {
        "section": section,
        "section_config": cfg.get(section) if section else None,
        "config": _all_sections(cfg),
        "writable_sections": sorted(sgme_config.CONFIG_SECTIONS),
    }


def _unknown_section_verbose(section: str) -> str:
    """未知段文案（**带**可用段列表）。

    v0.6 用在两处：HTTP ``GET /v1/admin/config/{section}``、
    HTTP 更新的**单段**形态。
    """
    return f"未知配置段: {section}（可用: {sorted(sgme_config.CONFIG_SECTIONS)}）"


def _unknown_section_short(section: str) -> str:
    """未知段文案（**不带**可用段列表）。

    v0.6 用在两处：HTTP 更新的**多段**形态、MCP ``config_update``。
    同一语义两种文案，是 v0.6 的既有不一致，v0.8 统一。
    """
    return f"未知配置段: {section}"


# ---------- 操作 1：读取配置（宽松版，MCP config_get / HTTP 全量读） ----------

def get_config(cfg: dict[str, Any], *, section: str | None = None) -> OperationResult:
    """读取运行时配置（**不校验** section 是否已注册）。

    宽松语义来自 MCP ``config_get`` 的 v0.6 实现——它对未知段不报错，
    直接回 ``config: null``。HTTP 的**全量读**（无 section）也走这里，
    因为无 section 时本就没有可校验对象。

    Args:
        cfg: 运行时配置字典。
        section: 段名；None / 空串表示整体读（v0.6 MCP 用 ``if section:``
            真值判断，空串等同于整体读，此处照抄该口径）。

    Returns:
        ``OperationResult(ok=True)``，data 见 ``_snapshot``。本操作不失败。
    """
    return OperationResult.succeed(_snapshot(cfg, section or None))


def get_config_section(cfg: dict[str, Any], *, section: str) -> OperationResult:
    """读取单个配置段（**严格版**：未知段 → ERR_NOT_FOUND）。

    严格语义来自 HTTP ``GET /v1/admin/config/{section}`` 的 v0.6 实现。
    校验通过后直接委托宽松版，保证两者的成功态完全同源。

    Args:
        cfg: 运行时配置字典。
        section: 段名（必填）。

    Returns:
        成功同 ``get_config``；未知段返回 ``ok=False, error_code=ERR_NOT_FOUND``
        （details 留空——v0.6 的 404 体只有 code/message 两键）。
    """
    if section not in sgme_config.CONFIG_SECTIONS:
        return OperationResult.fail(ERR_NOT_FOUND, _unknown_section_verbose(section))
    return get_config(cfg, section=section)


def get_http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP 读配置的历史契约形态（v0.6 逐字段等价）。

    两种形态由 ``data["section"]`` 选择（分流只允许出现在投影函数里）：
    - 整体读 ``GET /v1/admin/config`` → ``{"config", "writable_sections"}``
    - 单段读 ``GET /v1/admin/config/{section}`` → ``{"section", "config"}``
    """
    if data["section"] is None:
        return {
            "config": data["config"],
            "writable_sections": data["writable_sections"],
        }
    return {"section": data["section"], "config": data["section_config"]}


def get_mcp_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 MCP ``config_get`` 的历史契约形态（v0.6 逐字段等价）。

    与 ``get_http_payload`` 的唯一差异：整体读**不带** ``writable_sections``。
    """
    if data["section"] is None:
        return {"config": data["config"]}
    return {"section": data["section"], "config": data["section_config"]}


# ---------- 操作 2：更新配置 ----------

def _apply_values(cfg: dict[str, Any], section: str, values: dict[str, Any]) -> None:
    """把 values 按段白名单过滤后合并进 cfg（不落盘）。

    单段更新路径专用——v0.6 单段分支确实先 ``filter_keys`` 再 ``apply_section``。
    多段分支（HTTP section=None）**不过滤**，属既有不一致，见 ``update_config``。
    """
    filtered = sgme_config.filter_keys(section, values)
    sgme_config.apply_section(cfg, section, filtered)


def update_config(
    cfg: dict[str, Any],
    *,
    section: str | None = None,
    values: dict[str, Any] | None = None,
) -> OperationResult:
    """更新配置（**HTTP 版**：严格文案 + 落盘异常上抛）。

    v0.6 ``routes_config._do_update_config`` 逐行保留，含两处刻意保留的怪癖：

    1. **多段形态不过滤白名单键**。``section=None`` 时 values 的键即段名，
       直接 ``apply_section``，不走 ``filter_keys``；单段形态才过滤。
       这是 v0.6 的既有不一致，抽取时不得"顺手对齐"。
    2. **多段形态边改边校验**。逐个段 apply，遇到未知段才报错——此前已 apply
       的段留在内存 cfg 里（但因未 persist，不落盘）。行为原样保留。

    落盘异常（磁盘满 / 目录写保护等）**不捕获**，向上抛给入口层全局异常
    处理器，还原 v0.6 的 500 ``内部错误: {e}`` 形态。

    Args:
        cfg: 运行时配置字典（**就地修改**，与 v0.6 一致）。
        section: 目标段名；None 表示「多段一次改」形态。
        values: 待写入的键值；None 归一为空 dict。

    Returns:
        成功时 data 见 ``_snapshot``（额外带 ``status="ok"``）；
        未知段返回 ok=False（多段形态 → ERR_INVALID_ARGS/400；
        单段形态 → ERR_NOT_FOUND/404，与 v0.6 的状态码一致）。
    """
    vals: dict[str, Any] = values or {}

    if section is None:
        for sec, sec_vals in vals.items():
            if sec not in sgme_config.CONFIG_SECTIONS:
                return OperationResult.fail(ERR_INVALID_ARGS, _unknown_section_short(sec))
            sgme_config.apply_section(
                cfg, sec, sec_vals if isinstance(sec_vals, dict) else {},
            )
        sgme_config.persist_config(cfg)
        return OperationResult.succeed({"status": "ok", **_snapshot(cfg, None)})

    if section not in sgme_config.CONFIG_SECTIONS:
        return OperationResult.fail(ERR_NOT_FOUND, _unknown_section_verbose(section))
    _apply_values(cfg, section, vals)
    sgme_config.persist_config(cfg)
    return OperationResult.succeed({"status": "ok", **_snapshot(cfg, section)})


def update_config_section(
    cfg: dict[str, Any],
    *,
    section: str,
    values: dict[str, Any] | None = None,
) -> OperationResult:
    """更新单个配置段（**MCP 版**：简短文案 + 落盘异常转失败态）。

    与 ``update_config`` 单段分支的**唯一**两处差异（均为 v0.6 既有行为）：

    1. 未知段文案不带可用段列表（``未知配置段: {s}``）；
    2. ``persist_config`` 失败**就地转为失败态** ``配置落盘失败: {e}``，
       而不是上抛——v0.6 MCP ``config_update`` 正是这么写的。

    关于第 2 点与「禁 catch-all except」的关系：这里的 ``except`` **只包住
    ``persist_config`` 一次调用**，不是包住整个操作体，且捕获范围与 v0.6
    完全一致。放宽或收紧都会改变 MCP 的错误响应形态，与「API 契约不变」冲突。

    Args:
        cfg: 运行时配置字典（就地修改）。
        section: 目标段名（必填）。
        values: 待写入的键值；None 归一为空 dict。

    Returns:
        成功时 data 见 ``_snapshot``（额外带 ``status="ok"``）；
        未知段 / 落盘失败返回 ok=False。
    """
    if section not in sgme_config.CONFIG_SECTIONS:
        return OperationResult.fail(ERR_NOT_FOUND, _unknown_section_short(section))
    _apply_values(cfg, section, values or {})
    try:
        sgme_config.persist_config(cfg)
    except Exception as e:  # noqa: BLE001 —— v0.6 MCP 行为：落盘失败回 error JSON
        return OperationResult.fail(ERR_INTERNAL, f"配置落盘失败: {e}")
    return OperationResult.succeed({"status": "ok", **_snapshot(cfg, section)})


def update_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为「更新配置」的历史契约形态（HTTP 与 MCP 共用，v0.6 逐字段等价）。

    两种形态由 ``data["section"]`` 选择：
    - 多段形态（HTTP 专有）→ ``{"status", "config"}``（全量段快照）
    - 单段形态（HTTP / MCP 同形）→ ``{"status", "section", "config"}``（单段内容）

    两端在单段形态下响应恰好一致，故不拆 http/mcp 两个函数；
    多段形态 MCP 侧不存在（其 section 必填），走不到该分支。
    """
    if data["section"] is None:
        return {"status": data["status"], "config": data["config"]}
    return {
        "status": data["status"],
        "section": data["section"],
        "config": data["section_config"],
    }
