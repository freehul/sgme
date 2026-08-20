"""operations/update_check.py：服务端版本检测（ST-34 T-91）。

职责：检测 GitHub Releases 是否有新版本，供 health 端点 / WebUI 引导用户更新。

设计要点：
- **GitHub 优先**（决策 2026-08-21）：默认查 https://api.github.com/repos/freehul/sgme/releases/latest
  （公开仓库免 token），失败静默降级不抛异常，不拖垮服务。
- **语义化对比**：解析 tag_name（如 v1.0.0b5）与当前版本比较；预发布版本
  （b4 < b5）视为新版本；正式版 > 预发布。
- **可配置**：config/sgme.yaml 加 update_check 段（enabled / interval_hours / source）。
- **无副作用**：只读外部 API，不写库不调 LLM，失败不影响主流程。

对外接口：
- ``check_latest_version(current, cfg) -> dict``：返回 {update_available, latest_version,
  update_checked_at, update_error}，供 health 端点组装。
- ``DEFAULT_UPDATE_CHECK_CONFIG``：默认配置段。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("sgme.operations.update_check")

# 默认配置段（load_sgme_config 缺失时兜底；用户可在 sgme.yaml 覆盖）
DEFAULT_UPDATE_CHECK_CONFIG: dict[str, Any] = {
    "enabled": True,
    "interval_hours": 24,
    "source": "github",  # github 优先；可换 gitee
}

# GitHub Releases API（公开仓库免 token）；Gitee 对应 API 留作 source 备选
_GITHUB_RELEASES_API = "https://api.github.com/repos/freehul/sgme/releases/latest"
_GITEE_RELEASES_API = "https://gitee.com/api/v5/repos/freehul/sgme/releases/latest"

# 版本号正则：v1.0.0b4 / 1.0.0 / v2.1.0rc1
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:([a-zA-Z]+)(\d+))?$")


def _parse_version(tag: str) -> tuple[int, int, int, str | None, int] | None:
    """解析版本 tag 为可比较元组 (major, minor, patch, pre_label, pre_num)。

    支持形态：
    - v1.0.0b4 → (1, 0, 0, "b", 4)
    - 1.0.0    → (1, 0, 0, None, 0)
    - v2.1.0rc1 → (2, 1, 0, "rc", 1)
    无法解析 → None（调用方容错，不抛异常）。
    """
    m = _VERSION_RE.match(tag.strip())
    if not m:
        return None
    major, minor, patch, pre_label, pre_num = m.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        pre_label or None,
        int(pre_num) if pre_num else 0,
    )


def _version_gt(a: str, b: str) -> bool:
    """语义化版本比较：a > b 返回 True。

    规则：
    - 预发布版本（带 b/rc 等标签）< 同号正式版
    - 同号预发布按标签数字比较（b4 < b5）
    - 无法解析 → False（容错）
    """
    pa = _parse_version(a)
    pb = _parse_version(b)
    if pa is None or pb is None:
        return False
    # 主版本/次版本/修订号
    for x, y in zip(pa[:3], pb[:3]):
        if x != y:
            return x > y
    # 同为正式版（无 pre 标签）→ 相等
    if pa[3] is None and pb[3] is None:
        return False
    # 一方有 pre 标签：有 pre 的 < 无 pre 的（正式版更大）
    if pa[3] is None:
        return True  # a 正式版，b 预发布
    if pb[3] is None:
        return False  # a 预发布，b 正式版
    # 双方都是预发布：先比标签字母，再比数字
    if pa[3] != pb[3]:
        return pa[3] > pb[3]
    return pa[4] > pb[4]


def _build_client() -> httpx.Client:
    """构造 HTTP 客户端（trust_env=False：不读系统代理，避免代理配置污染）。"""
    return httpx.Client(timeout=10.0, trust_env=False)


# ---------- 检查结果缓存（health 高频读，定时任务低频刷新） ----------
# 模块级缓存：health 每次调用读缓存（避免每次外部 API 请求）；
# 后台 update_check_task 按 interval_hours 定时 refresh() 刷新。
_cached_result: dict[str, Any] | None = None


def get_cached(current: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """读取缓存的最新版本检测结果。

    首次调用（无缓存）时同步执行一次检查；之后返回缓存，不重复请求外部 API。
    ``current`` 仅用于首次初始化（后续缓存自含 latest_version）。
    """
    global _cached_result
    if _cached_result is None:
        _cached_result = check_latest_version(current, cfg)
    return dict(_cached_result)


def refresh(current: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """主动刷新缓存（后台定时任务调用），返回最新结果。"""
    global _cached_result
    _cached_result = check_latest_version(current, cfg)
    return dict(_cached_result)


def check_latest_version(current: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """检测 GitHub Releases 是否有新版本。

    Args:
        current: 当前版本号（如 "1.0.0b4"）。
        cfg: 运行时配置（含 update_check 段）；None/缺段 → 默认。

    Returns:
        dict：{update_available, latest_version, update_checked_at, update_error}
        - update_available: bool，是否有可更新版本
        - latest_version: 最新版本号（无更新时 = current；检查失败时 = current）
        - update_checked_at: ISO 时间戳（UTC），本次检查时间
        - update_error: 检查失败原因（成功为 None）

    永不抛异常：任何网络/解析错误 → 静默降级（记录 update_error），不影响主流程。
    """
    now = datetime.now(timezone.utc).isoformat()
    uc = (cfg or {}).get("update_check") or {}
    if not uc.get("enabled", DEFAULT_UPDATE_CHECK_CONFIG["enabled"]):
        return {
            "update_available": False,
            "latest_version": current,
            "update_checked_at": now,
            "update_error": None,
        }

    source = uc.get("source", DEFAULT_UPDATE_CHECK_CONFIG["source"])
    api_url = _GITHUB_RELEASES_API if source == "github" else _GITEE_RELEASES_API

    try:
        client = _build_client()
        try:
            resp = client.get(api_url)
            resp.raise_for_status()
            data = resp.json()
        finally:
            client.close()

        latest_tag = (data or {}).get("tag_name", "")
        if not latest_tag:
            return {
                "update_available": False,
                "latest_version": current,
                "update_checked_at": now,
                "update_error": "release 响应缺 tag_name",
            }

        has_update = _version_gt(latest_tag, current)
        return {
            "update_available": has_update,
            "latest_version": latest_tag,
            "update_checked_at": now,
            "update_error": None,
        }
    except Exception as exc:  # noqa: BLE001 —— 静默降级：任何异常不抛出
        logger.warning("update check failed: %s", exc)
        return {
            "update_available": False,
            "latest_version": current,
            "update_checked_at": now,
            "update_error": str(exc),
        }
