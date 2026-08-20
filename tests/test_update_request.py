"""tests/test_update_request.py：ST-34 自动更新意图文件测试（T-93）。"""
from __future__ import annotations

import json

from sgme.operations import update_request


def test_write_and_read(tmp_path):
    """写入 → 读取回显一致。"""
    res = update_request.write_update_request(tmp_path, "v1.0.0b5")
    assert res.ok
    assert res.data["target_version"] == "v1.0.0b5"
    assert res.data["status"] == "pending"

    got = update_request.read_update_request(tmp_path)
    assert got["target_version"] == "v1.0.0b5"
    assert got["status"] == "pending"
    assert "requested_at" in got


def test_read_nonexistent_returns_empty(tmp_path):
    """无请求文件 → {}。"""
    assert update_request.read_update_request(tmp_path) == {}


def test_write_is_atomic(tmp_path):
    """原子写：无残留 .tmp 文件。"""
    update_request.write_update_request(tmp_path, "v1.0.0b5")
    tmp_files = list((tmp_path / "update").glob("*.tmp"))
    assert tmp_files == []


def test_write_failure_returns_fail(tmp_path, monkeypatch):
    """写失败 → OperationResult(ok=False)，不抛异常。"""
    from pathlib import Path

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    res = update_request.write_update_request(tmp_path, "v1.0.0b5")
    assert not res.ok
    assert res.error_code == "ERR_UPDATE_REQUEST_WRITE"


def test_clear(tmp_path):
    """清除 → 文件消失，幂等。"""
    update_request.write_update_request(tmp_path, "v1.0.0b5")
    assert update_request.read_update_request(tmp_path) != {}
    res = update_request.clear_update_request(tmp_path)
    assert res.ok
    assert update_request.read_update_request(tmp_path) == {}
    # 幂等：不存在也 ok
    res2 = update_request.clear_update_request(tmp_path)
    assert res2.ok


def test_corrupt_file_returns_empty(tmp_path):
    """损坏 JSON → {}（静默降级）。"""
    d = tmp_path / "update"
    d.mkdir(parents=True, exist_ok=True)
    (d / "request.json").write_text("{invalid json", encoding="utf-8")
    assert update_request.read_update_request(tmp_path) == {}
