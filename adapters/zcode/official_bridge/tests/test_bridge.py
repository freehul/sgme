# -*- coding: utf-8 -*-
"""official_bridge 单元测试：解析、CLI 调用、HTTP 映射（不依赖真实账号）。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "bridge.py"
FAKE_CLI = HERE / "fake_zcode_cli.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("zcode_official_bridge", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


bridge = load_bridge()


def test_extract_text_openai_style():
    raw = json.dumps({"choices": [{"message": {"content": "hello"}}]})
    assert bridge.extract_text(raw) == "hello"


def test_extract_text_message_parts():
    raw = json.dumps({"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}})
    assert bridge.extract_text(raw) == "ab"


def test_extract_text_plain_fallback():
    raw = "[log] starting\n思考中...\n最终答案\n[trace] done"
    assert "最终答案" in bridge.extract_text(raw)


def test_extract_text_risk_control():
    raw = '{"error":{"message":"blocked (405, code 3012)"}}'
    assert bridge.extract_text(raw) == ""


def test_build_prompt_roles():
    p = bridge.build_prompt([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ])
    assert "【系统指令】" in p and "sys" in p and "【用户输入】" in p and "hi" in p and "yo" in p


def _runner(monkeypatch, mode: str = "json", timeout: float = 30.0) -> object:
    monkeypatch.setenv("FAKE_ZCODE_MODE", mode)
    return bridge.CliRunner(node=sys.executable, cjs=str(FAKE_CLI), timeout_s=timeout, concurrency=1)


def test_runner_ok(monkeypatch):
    r = _runner(monkeypatch)
    res = r.run("测试提示词")
    assert res["ok"] is True
    assert "FAKE-ANSWER" in res["text"]


def test_runner_rate_limit(monkeypatch):
    r = _runner(monkeypatch, mode="rate")
    res = r.run("x")
    assert res["ok"] is False
    assert res["error_type"] == "rate_limit"


def test_runner_risk_control(monkeypatch):
    r = _runner(monkeypatch, mode="risk")
    res = r.run("x")
    assert res["ok"] is False
    assert res["error_type"] == "risk_control"


def test_runner_mcp_error(monkeypatch):
    r = _runner(monkeypatch, mode="mcp")
    res = r.run("x")
    assert res["ok"] is False
    assert res["error_type"] == "mcp_error"


def test_runner_timeout(monkeypatch):
    r = _runner(monkeypatch, mode="slow", timeout=1.0)
    res = r.run("x")
    assert res["ok"] is False
    assert res["error_type"] == "timeout"


def _start_server(monkeypatch, mode: str = "json"):
    monkeypatch.setenv("FAKE_ZCODE_MODE", mode)
    runner = bridge.CliRunner(node=sys.executable, cjs=str(FAKE_CLI), timeout_s=30, concurrency=2)
    from http.server import ThreadingHTTPServer
    handler = type("H", (bridge.BridgeHandler,), {"runner": runner, "model_id": "zcode/GLM-5.3-Flash-Trial"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def _post(port: int, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_ok(monkeypatch):
    httpd, port = _start_server(monkeypatch)
    try:
        code, body = _post(port, {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert code == 200
        assert "FAKE-ANSWER" in body["choices"][0]["message"]["content"]
    finally:
        httpd.shutdown()


def test_http_rate_limit(monkeypatch):
    httpd, port = _start_server(monkeypatch, mode="rate")
    try:
        code, body = _post(port, {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert code == 429
        assert body["error"]["type"] == "rate_limit"
    finally:
        httpd.shutdown()


def test_http_models(monkeypatch):
    httpd, port = _start_server(monkeypatch)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % port, timeout=10) as r:
            data = json.loads(r.read().decode())
        assert data["data"][0]["id"] == "zcode/GLM-5.3-Flash-Trial"
    finally:
        httpd.shutdown()
