"""v0.7 §13 测试：providers.yaml 供应商连接层与 llm.yaml 编排层合并。

覆盖：
- load_providers_config：解析 / 结构校验 / 文件缺失返回 {}
- load_llm_config 合并：链节点按 provider 名注入连接字段（节点内联优先）
- 未知供应商且无内联 base_url → ValueError
- providers.yaml 缺失 → 回退内联（向后兼容旧 llm.yaml 结构）
- 密钥铁律：providers.yaml 只存环境变量名，无明文 key
"""

from __future__ import annotations

import pytest

from sgme import config


# ---------- 测试数据 ----------

# 拆分格式：链节点只引用 provider 名 + 链级参数
LLM_WITH_PROVIDER_REF = """\
chains:
  refinement:
    - provider: deepseek
      model: deepseek-v4-flash
      max_tokens: 16384
      extra_body:
        thinking:
          type: disabled
    - provider: lm-studio
      model: qwen/qwen3.5-9b
      max_tokens: 16384
      sampling:
        temperature: 1.0
    - provider: rule
      rule: drop_batch
rules:
  timeout_s: 240
  max_retries: 2
  fallback_on: [timeout, 5xx]
  context:
    reserved_output: 4096
    prompt_overhead: 0.08
  allowed_models:
    deny_prefixes: [pro, reasoner, thinking]
    deny_exact: [gemma-4-12b-qat]
"""

PROVIDERS = """\
providers:
  deepseek:
    name: deepseek
    display_name: "DeepSeek 云端"
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY_SGME
    provider_type: openai_compat
    default_model: deepseek-v4-flash
    context_window: 1048576
    timeout_s: 120
    max_retries: 3
    health_endpoint: /models
    health_interval_s: 60
  lm-studio:
    name: lm-studio
    display_name: "LM Studio 本地"
    base_url: http://127.0.0.1:1014/v1
    provider_type: openai_compat
    default_model: qwen/qwen3.5-9b
    context_window: 65536
    timeout_s: 120
    max_retries: 3
    health_endpoint: /models
    health_interval_s: 60
"""


def _write(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------- providers.yaml 解析 ----------

def test_load_providers_default_file_structure_complete():
    """真实 providers.yaml：LLM 供应商连接字段齐全（不固化品牌，ST-22 提供商无关）。

    lm-studio 已移除（2026-08-14 用户决策）。主 LLM 供应商 = 含 context_window
    （LLM 链引用）；embedding 供应商（volc-plan/siliconflow/nvidia）并入 providers 段。
    断言验「字段结构 + 密钥铁律」，不绑定具体品牌——用户替换 providers.yaml 为
    任意 OpenAI 兼容提供商后本测试依然成立。
    """
    providers = config.load_providers_config()
    assert providers
    llm_providers = {n: p for n, p in providers.items() if p.get("context_window")}
    assert llm_providers, "至少一个 LLM 供应商（含 context_window 字段）"
    required = {"name", "display_name", "base_url", "provider_type",
                "default_model", "context_window", "timeout_s", "max_retries",
                "health_endpoint", "health_interval_s"}
    for pname, p in llm_providers.items():
        missing = required - set(p)
        assert not missing, f"LLM 供应商 {pname} 缺字段 {missing}"
        # 密钥铁律（#10）：api_key_env 只存环境变量名，禁止明文
        assert p.get("api_key_env"), f"LLM 供应商 {pname} 缺 api_key_env"


def test_load_providers_missing_file_returns_empty(tmp_path):
    """providers.yaml 缺失 → 返回 {}（触发 llm.yaml 内联回退兼容）。"""
    assert config.load_providers_config(tmp_path / "nope.yaml") == {}


def test_load_providers_malformed_raises(tmp_path):
    """providers.yaml 无顶层 providers 键 → ValueError。"""
    bad = _write(tmp_path, "providers_bad.yaml", "foo: bar\n")
    with pytest.raises(ValueError, match="providers.yaml"):
        config.load_providers_config(bad)


def test_load_providers_entry_missing_base_url_raises(tmp_path):
    """供应商节点缺 base_url → ValueError（连接层必备字段）。"""
    bad = _write(tmp_path, "providers_bad.yaml", "providers:\n  x:\n    name: x\n")
    with pytest.raises(ValueError, match="base_url"):
        config.load_providers_config(bad)


# ---------- 合并逻辑（链引用 provider → 注入连接字段） ----------

def test_merge_injects_connection_fields(tmp_path):
    """链节点引用 provider → 注入 base_url/api_key_env/context_window 等连接字段。"""
    llm = _write(tmp_path, "llm.yaml", LLM_WITH_PROVIDER_REF)
    prov = _write(tmp_path, "providers.yaml", PROVIDERS)
    cfg = config.load_llm_config(llm, prov)
    head = cfg["chains"]["refinement"][0]
    # 连接字段注入（内存结构与原内联格式一致）
    assert head["provider"] == "deepseek"
    assert head["base_url"] == "https://api.deepseek.com/v1"
    assert head["api_key_env"] == "DEEPSEEK_API_KEY_SGME"
    assert head["context_window"] == 1048576
    assert head["provider_type"] == "openai_compat"
    assert head["health_endpoint"] == "/models"
    # 链级参数保留在链内
    assert head["model"] == "deepseek-v4-flash"
    assert head["max_tokens"] == 16384
    assert head["extra_body"]["thinking"]["type"] == "disabled"
    # 第二级同样注入
    second = cfg["chains"]["refinement"][1]
    assert second["base_url"] == "http://127.0.0.1:1014/v1"
    assert second["context_window"] == 65536
    assert second["sampling"]["temperature"] == 1.0
    # rule 节点不受影响（不在供应商表中，跳过注入）
    assert cfg["chains"]["refinement"][2] == {"provider": "rule", "rule": "drop_batch"}


def test_merge_node_inline_fields_win(tmp_path):
    """节点内联连接字段优先于 providers 表（新旧格式可混用）。"""
    llm = _write(tmp_path, "llm.yaml", LLM_WITH_PROVIDER_REF.replace(
        "provider: deepseek",
        "provider: deepseek\n      base_url: http://inline.example/v1",
    ))
    prov = _write(tmp_path, "providers.yaml", PROVIDERS)
    cfg = config.load_llm_config(llm, prov)
    assert cfg["chains"]["refinement"][0]["base_url"] == "http://inline.example/v1"


def test_merge_unknown_provider_without_inline_raises(tmp_path):
    """链引用 providers.yaml 未定义的供应商且节点无内联 base_url → ValueError。"""
    llm = _write(tmp_path, "llm.yaml", LLM_WITH_PROVIDER_REF.replace("deepseek", "nope"))
    prov = _write(tmp_path, "providers.yaml", PROVIDERS)
    with pytest.raises(ValueError, match="未知供应商"):
        config.load_llm_config(llm, prov)


def test_merge_unknown_provider_with_inline_fallback_ok(tmp_path):
    """链引用未知供应商但节点带内联 base_url → 兼容加载（不注入、不报错）。"""
    llm = _write(tmp_path, "llm.yaml", LLM_WITH_PROVIDER_REF.replace(
        "provider: deepseek",
        "provider: nope\n      base_url: http://inline.example/v1",
    ))
    prov = _write(tmp_path, "providers.yaml", PROVIDERS)
    cfg = config.load_llm_config(llm, prov)
    assert cfg["chains"]["refinement"][0]["base_url"] == "http://inline.example/v1"


# ---------- providers.yaml 缺失 → 回退内联 ----------

def test_missing_providers_file_falls_back_to_inline(tmp_path):
    """providers.yaml 缺失 → 旧内联 llm.yaml 结构原样加载（向后兼容）。"""
    inline = LLM_WITH_PROVIDER_REF.replace(
        "provider: deepseek",
        "provider: deepseek\n      base_url: https://api.deepseek.com/v1\n"
        "      api_key_env: DEEPSEEK_API_KEY\n      context_window: 1048576",
    )
    llm = _write(tmp_path, "llm.yaml", inline)
    cfg = config.load_llm_config(llm, tmp_path / "nope_providers.yaml")
    head = cfg["chains"]["refinement"][0]
    assert head["base_url"] == "https://api.deepseek.com/v1"
    assert head["api_key_env"] == "DEEPSEEK_API_KEY"
    assert head["context_window"] == 1048576
    # rules 段原样保留
    assert cfg["rules"]["timeout_s"] == 240


# ---------- 真实配置集成（内存结构兼容） ----------

def test_real_config_memory_structure_compatible():
    """真实 llm.yaml + providers.yaml 合并后，链结构合法（首节点连接字段齐，末链 rule 兜底）。

    ST-22 提供商无关：不断言具体品牌/地址/窗口值，只验结构与降级链机制——
    用户替换 providers.yaml 为任意 OpenAI 兼容提供商后本测试依然成立。
    """
    cfg = config.load_llm_config()
    refinement = cfg["chains"]["refinement"]
    head = refinement[0]
    # extra_body 为可选采样字段（现链 agnes/siliconflow 节点均未定义），不入必填清单
    for k in ("provider", "model", "base_url", "api_key_env",
              "context_window", "max_tokens"):
        assert k in head, f"首链缺 {k}"
    assert head["provider"], "首链 provider 非空"
    assert head["base_url"], "首链 base_url 非空"
    assert head["api_key_env"], "首链 api_key_env 非空（环境变量名，铁律 #10）"
    assert head["context_window"] > 0
    assert head["max_tokens"] > 0
    # 末链为规则兜底（降级链机制本身，与品牌无关）
    last = refinement[-1]
    assert last["provider"] == "rule"
    assert last["rule"] == "drop_batch"


def test_providers_yaml_no_plaintext_keys():
    """密钥铁律：providers.yaml 只存环境变量名，禁止明文 key。"""
    providers = config.load_providers_config()
    assert providers
    for pname, p in providers.items():
        for k in p:
            assert k not in ("api_key", "key", "secret", "token"), \
                f"供应商 {pname} 含明文密钥字段 {k}"
        if "api_key_env" in p:
            v = p["api_key_env"]
            # 只允许环境变量名（非空、无空格、无密钥特征前缀）
            assert v and " " not in v and "sk-" not in v.lower(), \
                f"供应商 {pname} api_key_env 疑似明文: {v!r}"
