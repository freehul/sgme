# -*- coding: utf-8 -*-
"""配置解析：密钥优先级 / 默认值 / 脱敏 / 缺 key 边界。"""
from claude_sgme.config import Settings

STATE = {
    "base_url": "http://10.0.0.1:9910",
    "agent_id": "claude-code",
    "api_key": "agt_state",
}


def test_key_priority_env_state_generic():
    env_first = Settings.from_env(environ={"SGME_CLAUDE_KEY": "agt_env"}, state=STATE)
    assert env_first.api_key == "agt_env" and env_first.api_key_source == "env"
    state_fallback = Settings.from_env(environ={}, state=STATE)
    assert state_fallback.api_key == "agt_state" and state_fallback.api_key_source == "state"
    generic = Settings.from_env(environ={"SGME_AGENT_KEY": "agt_generic"}, state={})
    assert generic.api_key == "agt_generic" and generic.api_key_source == "env-generic"


def test_defaults_and_mcp_derivation():
    empty = Settings.from_env(environ={}, state={})
    assert empty.base_url == "http://127.0.0.1:9910"
    assert empty.mcp_url == "http://127.0.0.1:9913/mcp"
    assert empty.agent_id == "claude-code"
    derived = Settings.from_env(environ={"SGME_HTTP_URL": "http://10.0.0.1:9910"}, state={})
    assert derived.mcp_url == "http://10.0.0.1:9913/mcp"


def test_agent_id_prefers_deployment_state():
    settings = Settings.from_env(environ={"SGME_AGENT_ID": "other"}, state=STATE)
    assert settings.agent_id == "claude-code"


def test_repr_redacts_secret():
    settings = Settings.from_env(environ={"SGME_CLAUDE_KEY": "agt_secret_value"}, state={})
    assert "agt_secret_value" not in repr(settings)
    assert "env" in repr(settings)


def test_require_agent_key_actionable_error():
    settings = Settings.from_env(environ={}, state={})
    try:
        settings.require_agent_key()
        raise AssertionError("should raise without a key")
    except ValueError as exc:
        assert "SGME_CLAUDE_KEY" in str(exc)
