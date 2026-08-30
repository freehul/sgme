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


# ---------- T-121 客户端模式（本机不跑 SGME，纯远程接入端） ----------

def test_write_client_install_json_null_dirs_remote_host(reload_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """客户端模式：http 指向远程 host/port，data_dir/raw_dir 必须为 null。"""
    home = tmp_path / "sgme-home"
    monkeypatch.setattr(config, "SGME_HOME", home)
    config.write_client_install_json(host="192.168.10.10", port=9910)
    p = home / "install.json"
    assert p.exists(), f"install.json 未生成: {p}"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["http"]["host"] == "192.168.10.10"
    assert data["http"]["port"] == 9910
    assert data["data_dir"] is None, "客户端模式 data_dir 必须为 null（本地无数据目录，防服务发现误判）"
    assert data["raw_dir"] is None, "客户端模式 raw_dir 必须为 null（本地无数据目录，防服务发现误判）"
    assert isinstance(data["sgme_version"], str) and data["sgme_version"]


def test_write_client_install_json_keys_env_refs_no_secrets(reload_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """客户端模式：keys 仍为环境变量名引用，绝不落明文密钥（铁律 #10）。"""
    home = tmp_path / "sgme-home"
    monkeypatch.setattr(config, "SGME_HOME", home)
    monkeypatch.setenv("SGME_ADMIN_KEY", "client-secret-admin-value")
    config.write_client_install_json(host="192.168.10.10")
    content = (home / "install.json").read_text(encoding="utf-8")
    assert "client-secret-admin-value" not in content, "install.json 泄露了明文密钥！"
    data = json.loads(content)
    assert data["keys"]["admin"] == "SGME_ADMIN_KEY"
    assert data["keys"]["agent"] == "SGME_AGENT_KEY"
    assert data["keys"]["bearer"] == "SGME_BEARER_TOKEN"


def test_write_client_install_json_mcp_port_env_and_explicit(reload_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """客户端模式：mcp_port 未传取 SGME_MCP_PORT env（默认 9913），显式传参最高优先。"""
    home = tmp_path / "sgme-home"
    monkeypatch.setattr(config, "SGME_HOME", home)
    # 未设 env：默认 9913（与 write_install_json 同逻辑）
    monkeypatch.delenv("SGME_MCP_PORT", raising=False)
    config.write_client_install_json(host="192.168.10.10")
    data = json.loads((home / "install.json").read_text(encoding="utf-8"))
    assert data["mcp"]["port"] == 9913
    # env 生效：取 env 值
    monkeypatch.setenv("SGME_MCP_PORT", "9923")
    config.write_client_install_json(host="192.168.10.10")
    data = json.loads((home / "install.json").read_text(encoding="utf-8"))
    assert data["mcp"]["port"] == 9923
    # 显式传参：覆盖 env
    config.write_client_install_json(host="192.168.10.10", mcp_port=9933)
    data = json.loads((home / "install.json").read_text(encoding="utf-8"))
    assert data["mcp"]["port"] == 9933
