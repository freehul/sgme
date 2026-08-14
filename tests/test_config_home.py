"""T-23 标准安装布局：SGME_HOME 数据/配置重定向测试。

策略：模块级常量（DATA_DIR/RAW_DIR/DEFAULT_SGME_CONFIG/SECRETS_FILE/LOG_DIR）
在 import 时基于 SGME_HOME env 计算 → 用 monkeypatch.setenv + importlib.reload
验证两态：未设 SGME_HOME = 项目根（零回归）；设置后 = $SGME_HOME 下。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from sgme import config


@pytest.fixture
def reload_config(monkeypatch: pytest.MonkeyPatch):
    """返回一个函数：设置 env 后重载 config 模块，返回重载后的模块。

    测试结束自动 reload 回无 SGME_HOME 状态（防模块常量污染后续测试）。
    """
    def _reload(**env: str) -> object:
        for k in ("SGME_HOME", "SGME_CONFIG_PATH"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(config)

    yield _reload

    # teardown：先手动清 env（monkeypatch 还原在此之后才执行），再 reload 让常量跟随
    monkeypatch.delenv("SGME_HOME", raising=False)
    monkeypatch.delenv("SGME_CONFIG_PATH", raising=False)
    importlib.reload(config)


# ---------- 未设 SGME_HOME：零回归 ----------

def test_unset_sgme_home_keeps_project_root_paths(reload_config):
    """未设 SGME_HOME → data/raw/config 全在项目根（现状不变）。"""
    mod = reload_config()
    assert mod.SGME_HOME is None
    assert mod.DATA_DIR == mod.PROJECT_ROOT / "data"
    assert mod.RAW_DIR == mod.PROJECT_ROOT / "raw"
    assert mod.LOG_DIR == mod.PROJECT_ROOT / "logs"
    assert mod.DEFAULT_SGME_CONFIG == mod.PROJECT_ROOT / "config" / "sgme.yaml"
    assert mod.SECRETS_FILE == mod.PROJECT_ROOT / "config" / ".env"


def test_unset_sgme_home_load_config_paths_match(reload_config):
    """未设 SGME_HOME → load_config 的 paths 与项目根一致。"""
    mod = reload_config()
    cfg = mod.load_config()
    assert cfg["paths"]["data_dir"] == str(mod.PROJECT_ROOT / "data")
    assert cfg["paths"]["raw_dir"] == str(mod.PROJECT_ROOT / "raw")


# ---------- 设置 SGME_HOME：重定向生效 ----------

def test_sgme_home_redirects_user_paths(reload_config, tmp_path: Path):
    """设 SGME_HOME → data/raw/logs/config 落到 $SGME_HOME 下。"""
    home = tmp_path / "sgme-home"
    mod = reload_config(SGME_HOME=str(home))
    assert mod.SGME_HOME == home
    assert mod.DATA_DIR == home / "data"
    assert mod.RAW_DIR == home / "raw"
    assert mod.LOG_DIR == home / "logs"
    assert mod.DEFAULT_SGME_CONFIG == home / "config" / "sgme.yaml"
    assert mod.SECRETS_FILE == home / "config" / ".env"


def test_sgme_home_keeps_program_resources_at_project_root(reload_config, tmp_path: Path):
    """程序资源（llm.yaml/providers.yaml/registry）必须留在项目根，不跟随重定向。"""
    home = tmp_path / "sgme-home"
    mod = reload_config(SGME_HOME=str(home))
    assert mod.DEFAULT_LLM_CONFIG == mod.PROJECT_ROOT / "config" / "llm.yaml"
    assert mod.DEFAULT_PROVIDERS_CONFIG == mod.PROJECT_ROOT / "config" / "providers.yaml"
    assert mod.DEFAULT_DIMENSIONS_FILE == mod.PROJECT_ROOT / "registry" / "dimensions.yaml"


def test_sgme_home_load_config_paths_and_creates_data(reload_config, tmp_path: Path):
    """设 SGME_HOME → load_config 的 paths 指向 $SGME_HOME，且 data 目录被创建。"""
    home = tmp_path / "sgme-home"
    mod = reload_config(SGME_HOME=str(home))
    cfg = mod.load_config()
    assert cfg["paths"]["data_dir"] == str(home / "data")
    assert cfg["paths"]["raw_dir"] == str(home / "raw")
    assert (home / "data").is_dir()


def test_sgme_home_env_loaded_from_home_env_file(reload_config, tmp_path: Path):
    """SGME_HOME 下 config/.env 被 load_env_file 加载（密钥跟随用户目录）。"""
    home = tmp_path / "sgme-home"
    (home / "config").mkdir(parents=True)
    (home / "config" / ".env").write_text("T23_TEST_KEY=from-home-env\n", encoding="utf-8")
    mod = reload_config(SGME_HOME=str(home))
    assert mod.get_env("T23_TEST_KEY") == "from-home-env"


def test_sgme_home_load_sgme_config_from_home(reload_config, tmp_path: Path):
    """SGME_HOME 下 config/sgme.yaml 被 load_sgme_config 读取（用户配置跟随）。"""
    home = tmp_path / "sgme-home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "sgme.yaml").write_text(
        "l2:\n  max_scenes: 123\n", encoding="utf-8"
    )
    mod = reload_config(SGME_HOME=str(home))
    cfg = mod.load_sgme_config()
    assert cfg["l2"]["max_scenes"] == 123
