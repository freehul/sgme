# -*- coding: utf-8 -*-
"""tests/test_adapter_skill_cache.py：四家 CLI 适配器的 fetch 通道接入落盘缓存（T-406）。

覆盖 doubao / zcode / mimo / workbuddy（四家 ``sgme_client.py`` 同构）：
- ``fetch`` 首次未命中 → 下载 + 校验 + 原子落盘 + 写 ``.sgme-manifest.json``；
- 再次 ``fetch``（新客户端 = 新会话）**目录版本相同 → 零包传输**；
- 目录状态缺失但 ``package_sha256`` 相同 → 仍零包传输（二级命中）；
- 服务端内容变化 → 正常替换。

这些适配器是独立分发包，缓存实现是随包的 ``scripts/skill_cache.py`` 副本；
真源一致性由 ``tests/test_skill_cache_sync.py`` 守护。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.test_skill_cache_layer import make_package

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ("doubao", "zcode", "mimo", "workbuddy")
BASE = "http://127.0.0.1:9910"
SKILL = "demo-skill"
FILES = {"SKILL.md": b"# demo\n", "scripts/run.py": b"print(1)\n"}


def _load_client(host: str):
    """按宿主加载各自的 sgme_client（同名模块，用唯一模块名隔离）。"""
    scripts = ROOT / "adapters" / host / "scripts"
    path_added = str(scripts) not in sys.path
    if path_added:
        sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(f"sgme_client_{host}", scripts / "sgme_client.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        if path_added:
            sys.path.remove(str(scripts))


class _Resp:
    """urlopen 响应替身：二进制体 + 响应头 + 上下文协议。"""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeServer:
    """按路径路由的最小 SGME 服务端（只服务 D-59 三个端点）。"""

    def __init__(self, package: bytes, sha: str, files: int, catalog: str = "cat-v1"):
        self.package = package
        self.sha = sha
        self.files = files
        self.catalog = catalog
        self.calls: list[str] = []
        self.package_downloads = 0
        self.manifest_requests = 0

    def open(self, req, timeout=None):  # noqa: A002 - 对齐 urllib opener 接口
        path = str(getattr(req, "full_url", req)).split(BASE, 1)[-1]
        self.calls.append(path)
        if path == "/v1/skills/catalog-version":
            return _Resp(json.dumps({"catalog_version": self.catalog}).encode())
        if path == f"/v1/skills/{SKILL}/manifest":
            self.manifest_requests += 1
            return _Resp(json.dumps({
                "name": SKILL, "version": "1.0.0", "package_sha256": self.sha,
                "content_len": len(FILES["SKILL.md"]), "updated_at": "2026-11-02T00:00:00+08:00",
            }).encode())
        if path == f"/v1/skills/{SKILL}/package":
            self.package_downloads += 1
            return _Resp(self.package, {"X-Skill-Sha256": self.sha, "X-Skill-Files": str(self.files)})
        raise AssertionError(f"未预期的请求: {path}")


def _client(mod, server: _FakeServer):
    c = mod.SGME(base_url=BASE, key="agt_placeholder_for_tests")
    c._opener = server
    return c


@pytest.mark.parametrize("host", HOSTS)
def test_fetch_manifest_hit_skips_package_download(host, tmp_path):
    """D-59 验收核心：第二次 fetch 命中清单 → 不再下载技能包。"""
    mod = _load_client(host)
    raw, sha = make_package(SKILL, FILES)
    server = _FakeServer(raw, sha, len(FILES))

    first = _client(mod, server).fetch(SKILL, str(tmp_path))
    assert first["cached"] is False
    assert first["sha256"] == sha and first["files"] == len(FILES)
    assert (tmp_path / SKILL / "SKILL.md").is_file()
    manifest = json.loads((tmp_path / SKILL / ".sgme-manifest.json").read_text(encoding="utf-8"))
    assert manifest["content_hash"] == sha
    assert server.package_downloads == 1

    # 新客户端 = 新会话；目录版本未变 → 直接短路
    second = _client(mod, server).fetch(SKILL, str(tmp_path))
    assert second["cached"] is True
    assert server.package_downloads == 1, "命中后不得再下载技能包"
    assert second["path"] == first["path"]


@pytest.mark.parametrize("host", HOSTS)
def test_fetch_manifest_level_hit_when_catalog_state_missing(host, tmp_path):
    """目录状态被清（新机器/手工拷贝）时仍走二级比对：package_sha256 相同即命中。"""
    mod = _load_client(host)
    raw, sha = make_package(SKILL, FILES)
    server = _FakeServer(raw, sha, len(FILES))
    _client(mod, server).fetch(SKILL, str(tmp_path))
    (tmp_path / ".sgme-catalog.json").unlink()

    again = _client(mod, server).fetch(SKILL, str(tmp_path))
    assert again["cached"] is True
    assert server.package_downloads == 1
    assert server.manifest_requests == 2


@pytest.mark.parametrize("host", HOSTS)
def test_fetch_replaces_when_server_content_changes(host, tmp_path):
    """服务端内容变化 → 缓存失效并整体替换（旧文件不残留）。"""
    mod = _load_client(host)
    raw1, sha1 = make_package(SKILL, {"SKILL.md": b"# v1\n"})
    _client(mod, _FakeServer(raw1, sha1, 1)).fetch(SKILL, str(tmp_path))

    raw2, sha2 = make_package(SKILL, {"SKILL.md": b"# v2\n", "new.txt": b"new\n"})
    server2 = _FakeServer(raw2, sha2, 2, catalog="cat-v2")
    result = _client(mod, server2).fetch(SKILL, str(tmp_path))

    assert result["cached"] is False
    assert (tmp_path / SKILL / "SKILL.md").read_bytes() == b"# v2\n"
    assert (tmp_path / SKILL / "new.txt").is_file()
    assert server2.package_downloads == 1


def test_vendored_skill_cache_copy_is_present_for_all_cli_adapters():
    """四家分发包必须自带 skill_cache.py（安装后不能回指仓库）。"""
    for host in HOSTS:
        assert (ROOT / "adapters" / host / "scripts" / "skill_cache.py").is_file(), host
