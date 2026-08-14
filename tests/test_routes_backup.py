"""routes_backup 单测：backup.dir 落盘前校验（系统临时区告警）。

对应 Task #9 漏项2：_resolve_backup_dir 在最终目录位于 tempfile.gettempdir()
之下时应打 WARNING，防 HEAD 带临时路径时静默备份进回收区。
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from sgme.server import routes_backup


def test_resolve_backup_dir_warns_when_in_temp_zone(caplog):
    """backup.dir 落在系统临时区 → 打 WARNING（防静默备份进回收区）。"""
    caplog.set_level(logging.WARNING, logger="sgme.server.backup")
    temp_dir = Path(tempfile.gettempdir()) / "sgme_polluted_probe_zone"
    cfg = {"backup": {"dir": str(temp_dir)}}
    routes_backup._resolve_backup_dir(cfg)
    assert any(
        "系统临时区" in rec.message for rec in caplog.records
    ), "未观察到系统临时区 WARNING"


def test_resolve_backup_dir_no_warn_for_relative(caplog, monkeypatch):
    """相对路径（基于 USER_ROOT 非临时区）→ 不告警。

    SGME_HOME 隔离下 USER_ROOT 指向 tmp 临时区，须 monkeypatch 回项目根
    才能验证「相对路径基于非临时区」的原语义。
    """
    import sgme.config as sgme_config

    caplog.set_level(logging.WARNING, logger="sgme.server.backup")
    monkeypatch.setattr(sgme_config, "USER_ROOT", sgme_config.PROJECT_ROOT)
    cfg = {"backup": {"dir": "data/backups"}}
    routes_backup._resolve_backup_dir(cfg)
    assert not any(
        "系统临时区" in rec.message for rec in caplog.records
    ), "相对路径不应触发临时区告警"


def test_resolve_backup_dir_absolute_non_temp_no_warn(caplog):
    """绝对路径但不在临时区（如其他盘）→ 不告警（用户合法需求）。"""
    caplog.set_level(logging.WARNING, logger="sgme.server.backup")
    # 用一个明显不在系统临时区的绝对路径
    other = Path(tempfile.gettempdir()).parent / "sgme_persistent_backups"
    cfg = {"backup": {"dir": str(other)}}
    routes_backup._resolve_backup_dir(cfg)
    assert not any(
        "系统临时区" in rec.message for rec in caplog.records
    ), "非临时区绝对路径不应触发告警"
