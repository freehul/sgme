# -*- coding: utf-8 -*-
"""catalog 审核整改（PR #143917，2026-10-24 teknium1 审核）回归测试。

审核必改 3 项：
1. sgme_skill_materialize 落盘根收敛到 $HERMES_HOME/plugin-data/sgme/skills/
   （适配器锁死 + 服务端 allowed_roots 白名单兜底）
2. sgme_credential_get 默认关闭（工具面不下发 + 调用拒绝 + append 跳过其结果）
3. sgme_signal_claim/ack 的 event_id 走 _path_seg 转义
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from adapters.hermes.tests.test_plugin import (  # noqa: F401
    ADMIN_KEY,
    AGENT_KEY,
    BASE_URL,
    _Client,
    _Response,
    _provider,
    _wait_posts,
)


# ---------- 审核意见 1：materialize 固定落盘根 ----------

def test_materialize_locks_dest_dir_to_plugin_data_root():
    """dest_dir 由插件固定注入（plugin-data 根），模型传值被拒绝。"""
    p, fake = _provider(_Client(responses=[_Response(json_body={"path": "x", "sha256": "y"})]))
    p._hermes_home = "/tmp/fake-hermes-home"

    out = p.handle_tool_call("sgme_skill_materialize", {"name": "alpha"})
    assert json.loads(out).get("path") == "x"

    call = fake.calls[0]
    assert call["url"].endswith("/v1/skills/alpha/materialize")
    sent = call["json"]
    expected_root = str(Path("/tmp/fake-hermes-home/plugin-data/sgme/skills").resolve())
    assert sent["dest_dir"] == expected_root
    assert sent["allowed_roots"] == [expected_root]


def test_materialize_rejects_model_chosen_dest_dir():
    """模型自选 dest_dir → 结构化错误 + 不发任何请求。"""
    p, fake = _provider()
    p._hermes_home = "/tmp/fake-hermes-home"

    out = p.handle_tool_call(
        "sgme_skill_materialize", {"name": "alpha", "dest_dir": "/tmp/evil"}
    )
    assert "dest_dir" in json.loads(out)["error"]
    assert fake.calls == [], "拒绝路径不得触网"


def test_materialize_root_falls_back_to_default_hermes_home():
    """未 initialize（_hermes_home 空）时回落 ~/.hermes 默认主目录。"""
    p, _ = _provider()
    root = p._materialize_root()
    assert root.endswith(str(Path("plugin-data") / "sgme" / "skills"))
    assert Path(root).is_absolute()


# ---------- 审核意见 2：credential_get 默认关闭 ----------

def test_credential_get_hidden_and_guarded_by_default():
    """默认：工具面不含 credential_get；被直接调用返回结构化错误且不触网。"""
    p, fake = _provider()
    names = [s["name"] for s in p.get_tool_schemas()]
    assert "sgme_credential_get" not in names
    assert "sgme_credential_list" in names, "键名列表（不含值）保留"

    out = p.handle_tool_call("sgme_credential_get", {"name": "k"})
    assert "默认关闭" in json.loads(out)["error"]
    assert fake.calls == []


def test_credential_get_enabled_by_param_and_append_skips_results():
    """开启后恢复下发与调用；无论开关，其 tool 结果永不入 L0。"""
    p, fake = _provider(_Client(responses=[_Response(json_body={"value": "v"})]))
    p.credential_get_enabled = True
    names = [s["name"] for s in p.get_tool_schemas()]
    assert "sgme_credential_get" in names
    assert "SGME_ENABLE_CREDENTIAL_GET" in json.loads(
        p.handle_tool_call("sgme_credential_get", {"name": "k"})
    ).get("value", "") or fake.calls, "开启后正常调用"

    # append 跳过：credential_get 的 tool 消息不进 L0，其余照常
    p.capture_enabled = True
    p.sync_turn("q", "a", messages=[
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "name": "sgme_credential_get", "tool_call_id": "t1",
         "content": json.dumps({"value": "SECRET-CONTENT"})},
    ])
    _wait_posts(fake, 1)
    bodies = [c["json"] for c in fake.calls if c["url"].endswith("/v1/append")]
    assert len(bodies) == 1
    assert "SECRET-CONTENT" not in bodies[0]["content"]
    assert "sgme_credential_get" not in bodies[0]["content"]


def test_credential_get_env_flag(monkeypatch):
    """环境变量 SGME_ENABLE_CREDENTIAL_GET=1 可开启。"""
    import adapters.hermes as hermes_mod

    monkeypatch.setenv("SGME_ENABLE_CREDENTIAL_GET", "1")
    p = hermes_mod.SGMEProvider(base_url=BASE_URL, agent_key=AGENT_KEY, admin_key=ADMIN_KEY)
    assert p.credential_get_enabled is True
    monkeypatch.delenv("SGME_ENABLE_CREDENTIAL_GET")
    p2 = hermes_mod.SGMEProvider(base_url=BASE_URL, agent_key=AGENT_KEY, admin_key=ADMIN_KEY)
    assert p2.credential_get_enabled is False


# ---------- 审核意见 3：event_id 路径转义 ----------

def test_signal_claim_ack_encode_event_id():
    """event_id 含特殊字符时必须经 _path_seg 转义。"""
    from urllib.parse import quote

    p, fake = _provider(_Client(responses=[
        _Response(json_body={"claimed": True}),
        _Response(json_body={"acked": True}),
    ]))
    raw_id = "../../etc/passwd"
    p.handle_tool_call("sgme_signal_claim", {"event_id": raw_id})
    p.handle_tool_call("sgme_signal_ack", {"event_id": raw_id, "status": "acked"})

    urls = [c["url"] for c in fake.calls]
    assert any(quote(raw_id, safe="") in u and "/consume" in u for u in urls)
    assert any(quote(raw_id, safe="") in u and "/ack" in u for u in urls)
    for u in urls:
        assert "../../" not in u, f"裸 event_id 出现在 URL：{u}"


# ---------- config_schema：新增 credential_get_enabled 字段 ----------

def test_declared_schema_includes_credential_get_enabled():
    """声明式面板新增「允许读取凭据值」开关（默认关）。"""
    from adapters.hermes.tests.test_plugin import _load_declared_schema

    schema = _load_declared_schema()
    by = {f.key: f for f in schema.fields}
    assert "credential_get_enabled" in by
    field = by["credential_get_enabled"]
    assert field.kind == "bool" and field.default == "false"
    assert field.group == "行为偏好"


def test_apply_runtime_config_reads_credential_flag(tmp_path):
    """面板值（sgme/config.json）能开启/关闭 credential_get。"""
    p, _ = _provider()
    p._hermes_home = str(tmp_path)
    cfg_dir = tmp_path / "sgme"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"credential_get_enabled": True}), encoding="utf-8"
    )
    p.initialize("sess-cred-test", hermes_home=str(tmp_path))
    assert p.credential_get_enabled is True
