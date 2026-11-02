# -*- coding: utf-8 -*-
"""tests/test_hermes_skill_cache.py：hermes 适配器接入技能落盘缓存（D-59 / T-406）。

验证 D-60 对 hermes 的改造项：``sgme_skill_materialize`` 从「只写服务端固定根」
改为「包通道 + 本地缓存」——落盘在 ``$HERMES_HOME/plugin-data/sgme/skills/``，
第二次调用命中本地清单时**不再下载技能包**（D-59 验收核心）。

工具名/参数保持零变更；旧版 SGME（无包通道）的回退路径见
``tests/test_hermes_adapter.py::test_skill_materialize_fixed_root_and_rejects_dest_dir``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import adapters.hermes as hermes_mod
from adapters.hermes import SGMEProvider

from tests.test_skill_cache_layer import make_package

SKILL = "demo-skill"
FILES = {"SKILL.md": b"# demo\n", "scripts/run.py": b"print(1)\n"}


class _CacheResponse:
    """httpx.Response 的最小替身：状态码 + json + content + headers。"""

    def __init__(self, status_code: int = 200, json_body=None, content: bytes = b"",
                 headers: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body
        self.content = content
        self.headers = headers or {}
        self.text = json.dumps(json_body) if json_body is not None else ""

    def json(self):
        return self._json_body


class _CacheClient:
    """按路径路由的假客户端：只服务 D-59 三个端点，并统计包下载次数。"""

    def __init__(self, package: bytes, sha: str, files: int):
        self.package = package
        self.sha = sha
        self.files = files
        self.calls: list[str] = []
        self.package_requests = 0
        self.manifest_requests = 0
        self.is_closed = False

    def get(self, url: str, **kwargs):
        path = url.split("9910", 1)[-1]
        self.calls.append(path)
        if path == "/v1/skills/catalog-version":
            return _CacheResponse(json_body={"catalog_version": "catalog-v1"})
        if path == f"/v1/skills/{SKILL}/manifest":
            self.manifest_requests += 1
            return _CacheResponse(json_body={
                "name": SKILL, "version": "1.0.0", "package_sha256": self.sha,
                "content_len": len(FILES["SKILL.md"]), "updated_at": "2026-11-02T00:00:00+08:00",
            })
        if path == f"/v1/skills/{SKILL}/package":
            self.package_requests += 1
            return _CacheResponse(content=self.package,
                                  headers={"X-Skill-Sha256": self.sha, "X-Skill-Files": str(self.files)})
        return _CacheResponse(status_code=404, json_body={"detail": "not found"})

    def post(self, url: str, **kwargs):  # 不应被调用（新路径不再走服务端 materialize）
        raise AssertionError(f"新版 materialize 不应再 POST: {url}")


@pytest.fixture
def provider(tmp_path) -> tuple[SGMEProvider, _CacheClient]:
    raw, sha = make_package(SKILL, FILES)
    client = _CacheClient(raw, sha, len(FILES))
    p = SGMEProvider(base_url="http://127.0.0.1:9910", agent_key="agent-key-x",
                     admin_key="admin-key-x")
    p._client = client  # type: ignore[assignment]
    p._available = True
    p._hermes_home = str(tmp_path)
    return p, client


def test_materialize_lands_locally_and_second_call_skips_download(provider):
    """首次下载落本地；第二次 manifest 命中 → 零包传输（package_requests 不变）。"""
    p, client = provider
    first = json.loads(p.handle_tool_call("sgme_skill_materialize", {"name": SKILL}))

    root = p._materialize_root()
    assert first["channel"] == "package-cache"
    assert first["path"] == str(Path(root) / SKILL)
    assert (Path(root) / SKILL / "SKILL.md").read_bytes() == FILES["SKILL.md"]
    assert first["cached"] is False
    assert client.package_requests == 1
    manifest = json.loads((Path(root) / SKILL / ".sgme-manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["content_hash"] == first["sha256"]

    second = json.loads(p.handle_tool_call("sgme_skill_materialize", {"name": SKILL}))
    assert second["cached"] is True
    assert client.package_requests == 1, "命中后不得再下载技能包"
    assert second["sha256"] == first["sha256"]


def test_materialize_reports_error_when_package_corrupt(provider, tmp_path):
    """包校验失败 → 结构化错误（不抛异常，不污染旧副本）。"""
    p, _client = provider
    p.handle_tool_call("sgme_skill_materialize", {"name": SKILL})
    bad, bad_sha = make_package(SKILL, {"SKILL.md": b"# v2\n"}, tamper=True)
    p._client = _CacheClient(bad, bad_sha, 1)  # type: ignore[assignment]
    # 服务端目录版本变化，强制重新拉取（否则一级短路会命中旧副本）
    (Path(p._materialize_root()) / ".sgme-catalog.json").write_text(
        '{"catalog_version": "old"}', encoding="utf-8")

    out = json.loads(p.handle_tool_call("sgme_skill_materialize", {"name": SKILL}))
    assert "error" in out and "sha256 校验失败" in out["error"]
    assert (Path(p._materialize_root()) / SKILL / "SKILL.md").read_bytes() == FILES["SKILL.md"]


def test_materialize_falls_back_to_server_when_package_endpoint_missing(provider):
    """旧版 SGME（包通道 404）→ 回退服务端 materialize（接口面不变）。"""
    p, _client = provider

    class _OldServer(_CacheClient):
        def get(self, url: str, **kwargs):
            path = url.split("9910", 1)[-1]
            self.calls.append(path)
            if path.startswith("/v1/skills") and path.endswith("/package"):
                return _CacheResponse(status_code=404, json_body={"detail": "not found"})
            return _CacheResponse(status_code=404, json_body={"detail": "not found"})

        def post(self, url: str, **kwargs):
            return _CacheResponse(json_body={"path": "/server/side/demo-skill/SKILL.md",
                                             "sha256": "ab" * 32})

    old = _OldServer(b"", "0" * 64, 0)
    p._client = old  # type: ignore[assignment]
    out = json.loads(p.handle_tool_call("sgme_skill_materialize", {"name": SKILL}))
    assert out.get("path") == "/server/side/demo-skill/SKILL.md"


def test_tool_name_and_params_unchanged():
    """接口面零变更：工具名与参数集保持原样。"""
    p = SGMEProvider(base_url="http://127.0.0.1:9910")
    schema = next(s for s in p.get_tool_schemas() if s["name"] == "sgme_skill_materialize")
    assert set(schema["parameters"]["properties"]) == {"name", "dest_dir"}
    # 保持既有 schema 形状（dest_dir 参数仍在，只是值会被拒绝——接口面零变更）
    assert schema["parameters"]["required"] == ["name", "dest_dir"]
    assert hermes_mod._skill_cache is not None, "插件必须随包携带 skill_cache 副本"
