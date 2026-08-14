"""pytest 全局配置：测试环境禁用 MCP Server 自托管（避免 9911 端口冲突）。"""
from __future__ import annotations

import importlib
import os

import pytest

os.environ.setdefault("SGME_MCP_DISABLED", "1")


@pytest.fixture(autouse=True)
def _isolate_data_home(tmp_path, monkeypatch):
    """全局兜底：所有测试把 SGME_HOME 指向 tmp_path，数据目录全隔离。

    根治（2026-08-12）：漏带 raw_dir/conns fixture 的测试默认 DATA_DIR/RAW_DIR
    落生产项目根，直接写生产 raw/sessions（122 个 sess_test 测试文件实锤污染）。
    SGME_HOME 重定向后 data/raw/logs/config 全在 tmp，即使测试漏隔离也不碰生产。
    reload config 使模块级常量跟随 env（SGME_HOME 在 import 时读取）。
    与 _isolate_repo_config（配置路径）/ _isolate_logs（日志路径）互补，
    本 fixture 兜底数据/原始层目录。
    """
    from sgme import config as config_mod

    monkeypatch.setenv("SGME_HOME", str(tmp_path / "sgme_home"))
    importlib.reload(config_mod)
    yield
    # teardown：reload 使常量与还原后的 env 一致（monkeypatch 自动还原 env）
    importlib.reload(config_mod)


@pytest.fixture(autouse=True)
def _isolate_repo_config(tmp_path, monkeypatch):
    """全局兜底：所有测试把 SGME_CONFIG_PATH 指向本次测试的 tmp_path。

    防止任何测试（含未来新增文件自带 app fixture 调 /v1/admin/config）写回真实
    config/sgme.yaml——根治 Task #6 / #9 的"漏掉某个文件 fixture"类失效模式。
    各 app fixture 内同值 setenv 覆盖无害（防御纵深）。
    """
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    """日志隔离（2026-08-11）：测试进程的 sgme 日志输出重定向到 tmp。

    生产日志路径 logs/sgme.log 曾被 pytest 进程污染（19:15 起全量测试期间
    测试日志与生产日志混写同一文件，生产排查被淹没）。经 log/setup 的
    SGME_LOG_OUTPUT env 覆盖（优先级最高），测试绝不触碰生产日志文件。
    """
    monkeypatch.setenv("SGME_LOG_OUTPUT", str(tmp_path / "sgme_test.log"))
