"""l3 段配置合并（2.0 D-28/D-29 · T-320）。

要点：`enabled` 默认 **关**——配置段缺失时不改变现网裁决路径；
阈值只接受 0–1 的数值，非法值回退默认，未知键不进运行期配置。
"""

from __future__ import annotations

from sgme import config as cfg


def test_default_disabled():
    merged = cfg._merge_l3_config(None)
    assert merged["rules"]["enabled"] is False
    assert merged["rules"]["overlap_gray"] == 0.30
    assert merged["rules"]["overlap_same"] == 0.85


def test_enable_and_thresholds_passthrough():
    merged = cfg._merge_l3_config({"rules": {"enabled": True, "overlap_gray": 0.4}})
    assert merged["rules"]["enabled"] is True
    assert merged["rules"]["overlap_gray"] == 0.4
    assert merged["rules"]["overlap_same"] == 0.85, "未覆盖的阈值保留默认"


def test_invalid_values_fall_back():
    merged = cfg._merge_l3_config({
        "rules": {"enabled": "yes", "overlap_gray": 1.5, "overlap_same": -0.2, "oops": 1},
    })
    assert merged["rules"]["enabled"] is False, "非 bool 不得开启"
    assert merged["rules"]["overlap_gray"] == 0.30, "越界值回退默认"
    assert merged["rules"]["overlap_same"] == 0.85
    assert "oops" not in merged["rules"], "未知键不进运行期"


def test_loaded_config_has_l3_section(tmp_path, monkeypatch):
    """load_config 组装结果里必须有 l3 段（此前不在白名单 → l15 读不到，开关形同虚设）。"""
    loaded = cfg.load_config()
    assert "l3" in loaded
    assert loaded["l3"]["rules"]["enabled"] is False
