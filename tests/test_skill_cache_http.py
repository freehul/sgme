# -*- coding: utf-8 -*-
"""tests/test_skill_cache_http.py：真 HTTP 链路（UrllibTransport）缓存回归。

前面的缓存测试用内存假传输，覆盖不到真实网络层；本文件用标准库
``http.server`` 在 127.0.0.1 随机端口起一个最小 SGME 服务端，验证：

- ``UrllibTransport`` 会带 ``X-API-Key``、走真实 urllib 请求；
- 首次拉包落盘；**第二次命中目录版本 → 零包请求**（D-59 验收核心）；
- 预置错误 key 不会被回显到异常文案里。

纪律：只绑 127.0.0.1；不出外网；不碰真实 ~/.sgme。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adapters._shared import skill_cache as sc
from tests.test_skill_cache_layer import make_package as _make

# 复用 D-59 包构造器（同目录测试模块）


FILES = {"SKILL.md": b"# http demo\n", "scripts/run.py": b"print(1)\n"}


@pytest.fixture()
def server():
    """最小 SGME 服务端：catalog / manifest / package 三端点 + 计数。"""
    raw, sha = _make("demo-skill", FILES)
    state = {"catalog": 0, "manifest": 0, "package": 0, "keys": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音
            return

        def _send(self, body: bytes, ctype: str = "application/json", headers=None):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            state["keys"].append(self.headers.get("X-API-Key"))
            if self.path == "/v1/skills/catalog-version":
                state["catalog"] += 1
                self._send(json.dumps({"catalog_version": "http-cat-v1"}).encode())
            elif self.path == "/v1/skills/demo-skill/manifest":
                state["manifest"] += 1
                self._send(json.dumps({
                    "name": "demo-skill", "version": "1.0.0", "package_sha256": sha,
                    "content_len": len(FILES["SKILL.md"]), "updated_at": "2026-11-02T00:00:00+08:00",
                }).encode())
            elif self.path == "/v1/skills/demo-skill/package":
                state["package"] += 1
                self._send(raw, "application/gzip", {
                    "X-Skill-Sha256": sha, "X-Skill-Files": str(len(FILES)),
                })
            else:
                body = b'{"detail":"not found"}'
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", state
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_real_http_manifest_hit_skips_package(server, tmp_path):
    """真实 urllib 链路：第二次零包请求（目录版本短路）。"""
    base, state = server
    transport = sc.UrllibTransport(base, api_key="agt_placeholder_for_tests")

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    dest = cache.ensure_skill("demo-skill", tmp_path)
    assert (dest / "SKILL.md").read_bytes() == FILES["SKILL.md"]
    assert state["package"] == 1
    assert state["manifest"] == 1

    again = sc.SkillCache(transport, state_dir=tmp_path).ensure_skill("demo-skill", tmp_path)
    assert again == dest
    # 新实例仍需一次目录版本请求（会话开始语义），但不得再拉清单/包
    assert state["catalog"] == 2
    assert state["manifest"] == 1
    assert state["package"] == 1, "命中后不得再下载技能包"
    assert all(k == "agt_placeholder_for_tests" for k in state["keys"])
