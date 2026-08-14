"""T-23② install.json 服务发现清单测试（ST-23⑦ 落地）。

install.json 固定写 ~/.sgme/install.json（agent 发现 SGME 安装位置用），
内容 = 版本/HTTP 地址端口/MCP 端口/data_dir/raw_dir/Key 的环境变量名引用，
**不落任何明文密钥**。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from sgme import config


@pytest.fixture
def reload_config(monkeypatch: pytest.MonkeyPatch):
    """设置 env 后重载 config 模块（install.json 路径基于 SGME_HOME 计算）。

    测试结束自动 reload 回无 SGME_HOME 状态（防模块常量污染后续测试）。
    """
    def _reload(**env: str) -> object:
        for k in ("SGME_HOME",):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(config)

    yield _reload

    # teardown：先手动清 env（monkeypatch 还原在此之后才执行），再 reload 让常量跟随
    monkeypatch.delenv("SGME_HOME", raising=False)
    importlib.reload(config)


def test_write_install_json_creates_file(reload_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """write_install_json → ~/.sgme/install.json 生成，含全部字段。"""
    home = tmp_path / "sgme-home"
    monkeypatch.setattr(config, "SGME_HOME", home)
    monkeypatch.setattr(config, "USER_ROOT", home)
    cfg = {
        "paths": {
            "data_dir": str(home / "data"),
            "raw_dir": str(home / "raw"),
        },
    }
    config.write_install_json(cfg, host="127.0.0.1", port=9910)
    p = home / "install.json"
    assert p.exists(), f"install.json 未生成: {p}"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["http"]["host"] == "127.0.0.1"
    assert data["http"]["port"] == 9910
    assert data["mcp"]["port"] == 9913
    assert data["data_dir"] == str(home / "data")
    assert data["raw_dir"] == str(home / "raw")


def test_write_install_json_key_refs_env_names_not_secrets(reload_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """install.json 只写 key 的环境变量名引用，绝不落明文密钥。"""
    home = tmp_path / "sgme-home"
    monkeypatch.setattr(config, "SGME_HOME", home)
    monkeypatch.setattr(config, "USER_ROOT", home)
    monkeypatch.setenv("SGME_ADMIN_KEY", "supersecret-admin-value")
    monkeypatch.setenv("SGME_AGENT_KEY", "supersecret-agent-value")
    cfg = {"paths": {"data_dir": str(home / "data"), "raw_dir": str(home / "raw")}}
    config.write_install_json(cfg, host="127.0.0.1", port=9910)
    content = (home / "install.json").read_text(encoding="utf-8")
    assert "supersecret" not in content, "install.json 泄露了明文密钥！"
    data = json.loads(content)
    assert data["keys"]["admin"] == "SGME_ADMIN_KEY"
    assert data["keys"]["agent"] == "SGME_AGENT_KEY"


def test_write_install_json_respects_env_port(reload_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SGME_PORT / SGME_MCP_PORT env 生效时写入对应端口。"""
    home = tmp_path / "sgme-home"
    monkeypatch.setattr(config, "SGME_HOME", home)
    monkeypatch.setattr(config, "USER_ROOT", home)
    monkeypatch.setenv("SGME_PORT", "9921")
    monkeypatch.setenv("SGME_MCP_PORT", "9922")
    cfg = {"paths": {"data_dir": str(home / "data"), "raw_dir": str(home / "raw")}}
    config.write_install_json(cfg, host="0.0.0.0", port=9921)
    data = json.loads((home / "install.json").read_text(encoding="utf-8"))
    assert data["http"]["port"] == 9921
    assert data["mcp"]["port"] == 9922
