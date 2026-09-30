import pytest
from codex_sgme.config import Settings


def test_settings_read_remote_endpoints_and_safe_repr():
    settings = Settings.from_env(
        {
            "SGME_BASE_URL": "http://10.0.0.10:9910/",
            "SGME_AGENT_KEY": "secret-value",
            "SGME_AGENT_ID": "codex",
        }
    )

    assert settings.base_url == "http://10.0.0.10:9910"
    assert settings.mcp_url == "http://10.0.0.10:9913/mcp"
    assert settings.agent_id == "codex"
    assert "secret-value" not in repr(settings)


def test_settings_reject_invalid_base_url():
    with pytest.raises(ValueError, match="http or https"):
        Settings.from_env({"SGME_BASE_URL": "file:///tmp/sgme"})


def test_settings_requires_agent_key_for_authenticated_calls():
    settings = Settings.from_env({})

    with pytest.raises(ValueError, match="SGME_AGENT_KEY"):
        settings.require_agent_key()


def test_codex_key_takes_precedence_over_generic_agent_key():
    settings = Settings.from_env(
        {"SGME_CODEX_KEY": "codex-key", "SGME_AGENT_KEY": "other-agent-key"}
    )

    assert settings.api_key == "codex-key"
