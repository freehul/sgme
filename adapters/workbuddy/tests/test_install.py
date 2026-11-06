# -*- coding: utf-8 -*-
"""install.py 部署完整性回归测试。

背景（2026-11-06 实锤）：T-406 起 ``sgme_client.py`` 依赖同级 ``skill_cache.py``，
但 ``install.py`` 的复制清单没跟上，部署副本直接 ImportError，CLI 兜底整条链路
不可用。本用例锁住两条不变式：

1. 真源 ``scripts/*.py`` 必须全部出现在部署副本（新增依赖不会再被漏掉）；
2. 契约配置由占位符模板渲染，渲染后不留占位符、仓库内不含真实地址。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
SCRIPTS = ADAPTER / "scripts"
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("wb_install_test", ADAPTER / "install.py")
assert _spec is not None and _spec.loader is not None
wb_install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb_install)


def test_install_copies_every_client_dependency(tmp_path):
    """真源 scripts 下每个公开 .py 都必须被部署，否则视为漏依赖。"""
    dest = wb_install.install(tmp_path)
    src_py = {p.name for p in SCRIPTS.glob("*.py") if not p.name.startswith("_")}
    dst_py = {p.name for p in (dest / "scripts").glob("*.py")}
    assert src_py, "真源 scripts 解析到 0 个 .py（口径失效？）"
    assert src_py <= dst_py, f"部署清单漏了客户端依赖: {sorted(src_py - dst_py)}"


def test_deploy_tools_renders_template_without_placeholders(tmp_path, monkeypatch):
    """契约配置从占位符模板渲染；已存在时默认保留用户自定义。"""
    monkeypatch.setattr(wb_install, "home_dir", lambda: tmp_path)
    http = "http://<SGME_HOST>:9910"
    mcp = "http://<SGME_HOST>:9913/mcp"

    notes = wb_install.deploy_tools(http, mcp)
    cfg_path = tmp_path / ".workbuddy" / "sgme-deploy.json"
    assert cfg_path.is_file(), "契约配置未生成"
    text = cfg_path.read_text(encoding="utf-8")
    assert "{{" not in text, "模板占位符未全部渲染"
    cfg = json.loads(text)
    assert cfg["service"]["http"] == http
    assert cfg["service"]["mcp"] == mcp
    assert (tmp_path / ".workbuddy" / "sgme-tools" / "align_deploy.py").is_file()
    assert any("模板渲染" in n for n in notes)

    # 默认不覆盖已有配置；只有 --refresh-contract 对应的 refresh=True 才重建
    cfg_path.write_text(json.dumps({"custom": True}), encoding="utf-8")
    wb_install.deploy_tools(http, mcp)
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"custom": True}
    wb_install.deploy_tools(http, mcp, refresh=True)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["service"]["http"] == http
