"""L1.5 F1 近似查重（near_dup）测试：配置段（T1.4）+ 判定四分支（T1.5）。

L1.5 P1（T-167 词法扩展）：_exact_dup_check 只拦完全相同 content，换措辞重复
漏过率 4.5%（副本库 400 条抽样 18 条）。F1 在落库前做全池向量近似检索（limit=1）：
score≥threshold 命中 → 内容进隔离表人工复核（不静默 skip）；灰区 [gray_zone, threshold)
只聚合计数 + 明细抽样（阈值校准回算用）。全部用例零网络：embed / vector_search 一律 mock。
"""

from __future__ import annotations

import logging

from sgme import config
from sgme.engine import l15


# ---------- 0. 配置透传与回退（T1.4） ----------

def test_load_config_exposes_l15_near_dup():
    """load_config 组装结果含 l15.near_dup（包内基线透传 + 三键齐全）。"""
    # SGME_HOME 隔离下默认路径已重定向到 tmp，显式指包内真实配置验证透传
    cfg = config.load_config(
        sgme_path=config.RESOURCE_ROOT / "config" / "sgme.yaml"
    )
    l15_cfg = cfg.get("l15", {})
    assert "near_dup" in l15_cfg
    nd = l15_cfg["near_dup"]
    # 出厂保守示值（部署覆盖层由主仓同步显式 true）
    assert nd["enabled"] is True
    assert nd["threshold"] == 0.92
    assert nd["gray_zone"] == 0.85


def test_near_dup_cfg_fallback_defaults():
    """空/缺段配置 → 出厂保守值；单键类型非法 → 该键回退（不静默降档）。"""
    factory = {"enabled": True, "threshold": 0.92, "gray_zone": 0.85}
    assert l15._near_dup_cfg(None) == factory
    assert l15._near_dup_cfg({}) == factory
    assert l15._near_dup_cfg({"l15": {}}) == factory
    # 部分键：enabled=false 生效，其余键回退出厂值
    assert l15._near_dup_cfg({"l15": {"near_dup": {"enabled": False}}}) == {
        "enabled": False, "threshold": 0.92, "gray_zone": 0.85,
    }
    # 类型非法：字符串/布尔 → 对应键回退出厂值
    assert l15._near_dup_cfg(
        {"l15": {"near_dup": {"threshold": "高", "gray_zone": True}}}
    ) == factory


def test_near_dup_cfg_invalid_fallback(caplog):
    """gray_zone >= threshold 非法 → WARNING + 回退全组出厂值。"""
    caplog.set_level(logging.WARNING, logger="sgme.engine.l15")
    nd = l15._near_dup_cfg(
        {"l15": {"near_dup": {"enabled": False, "threshold": 0.8, "gray_zone": 0.9}}}
    )
    assert nd == {"enabled": True, "threshold": 0.92, "gray_zone": 0.85}
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("near_dup" in m and "回退" in m for m in warns), warns


def test_config_merge_l15_near_dup():
    """_merge_l15_config：near_dup 默认段随白名单合并 + 逐键透传 + 防可变默认共享。"""
    base = config._merge_l15_config(None)
    assert base["near_dup"] == {"enabled": True, "threshold": 0.92, "gray_zone": 0.85}
    merged = config._merge_l15_config({"near_dup": {"threshold": 0.9}})
    assert merged["near_dup"] == {"enabled": True, "threshold": 0.9, "gray_zone": 0.85}
    # 防可变默认共享：合并产物不是默认段的同一对象
    assert base["near_dup"] is not config.DEFAULT_L15_CONFIG["near_dup"]
