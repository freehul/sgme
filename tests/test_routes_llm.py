"""tests/test_routes_llm.py：LLM 供应商与降级链端点测试。

覆盖：
1. ``GET /v1/admin/llm`` 返回 chains / rules / providers（只读）
2. ``GET /v1/admin/llm/health`` 返回逐供应商健康探测（monkeypatch probe 离线）
3. 鉴权：agent key 调 /v1/admin/llm → 403
4. operations 层：llm_status / llm_health（注入 probe，不触发真实网络）
5. 健壮性：缺 base_url 的供应商 → available=False 不抛异常

零真实网络：健康探测统一 monkeypatch 为假探测。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.operations import llm as llm_ops

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    from sgme import config as _c
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(_c, "RAW_DIR", raw)
    return raw


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 路由：GET /v1/admin/llm ----------

def test_llm_returns_status(app, client):
    resp = client.get("/v1/admin/llm", headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert "chains" in data
    assert "rules" in data
    assert "providers" in data
    # 链结构与 llm.yaml 一致：refinement 链存在
    assert "refinement" in data["chains"]


def test_llm_requires_admin(app, client):
    resp = client.get("/v1/admin/llm", headers=AGENT_HEADERS)
    assert resp.status_code == 403


# ---------- 路由：GET /v1/admin/llm/health（离线） ----------

def test_llm_health_returns_probe(app, client, monkeypatch):
    def fake_probe(info):
        return {"available": True, "latency_ms": 5, "provider": info.get("provider")}

    monkeypatch.setattr(llm_ops, "_probe_provider", fake_probe)
    resp = client.get("/v1/admin/llm/health", headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert "health" in data
    # llm.yaml 链含 deepseek 一个非 rule 供应商（lm-studio 已移除，2026-08-14 用户决策）
    assert "deepseek" in data["health"]
    assert "lm-studio" not in data["health"]


# ---------- operations 层 ----------

def test_llm_status_collects_providers():
    cfg = {
        "llm": {
            "chains": {
                "refinement": [
                    {"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://x/v1"},
                    {"provider": "lm-studio", "model": "qwen/qwen3.5-9b", "base_url": "http://127.0.0.1:1014/v1"},
                    {"provider": "rule", "rule": "drop_batch"},
                ]
            },
            "rules": {"timeout_s": 240},
        },
        "search": {"vector": {"provider": "volc-plan", "base_url": "https://ark/v3", "api_key_env": "VOLC_API_KEY"}},
    }
    res = llm_ops.llm_status(cfg)
    assert res.ok
    providers = res.data["providers"]
    # rule 不进入供应商列表
    assert "rule" not in providers
    assert "deepseek" in providers
    # 链透传
    assert res.data["chains"]["refinement"][0]["provider"] == "deepseek"


def test_llm_status_exposes_embedding_and_vector_current(prov_path):
    """llm_status 暴露向量提供商（embedding 段）+ 当前生效 provider（T-43）。"""
    cfg = {"llm": {"chains": {}}, "search": {"vector": {"provider": "siliconflow"}}}
    res = llm_ops.llm_status(cfg)
    assert res.ok
    assert res.data["vector_current"] == "siliconflow"
    assert "embedding" in res.data
    # providers.yaml 无 embedding 段（prov_path 只写了 providers:{}）→ 空表
    assert res.data["embedding"] == {}


def test_llm_status_embedding_reads_providers_file(tmp_path, monkeypatch):
    """embedding 段从 providers.yaml 顶层读取（T-43）。"""
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers: {}\n"
        "embedding:\n"
        "  volc-plan:\n"
        "    base_url: https://ark/v3\n"
        "    api_key_env: VOLC_API_KEY\n"
        "    default_model: doubao-embedding-vision\n"
        "    models: [doubao-embedding-vision]\n"
        "    display_name: 火山方舟\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", path)
    cfg = {"llm": {"chains": {}}, "search": {"vector": {"provider": "volc-plan"}}}
    res = llm_ops.llm_status(cfg)
    assert res.ok
    emb = res.data["embedding"]
    assert "volc-plan" in emb
    assert emb["volc-plan"]["default_model"] == "doubao-embedding-vision"
    assert emb["volc-plan"]["api_key_env"] == "VOLC_API_KEY"
    assert "models" in emb["volc-plan"]


def test_embedding_set_active_persists(tmp_path, monkeypatch):
    """切换向量提供商写回 search.vector 并落盘（T-43）。"""
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers: {}\n"
        "embedding:\n"
        "  volc-plan:\n"
        "    base_url: https://ark/v3\n"
        "    api_key_env: VOLC_API_KEY\n"
        "    default_model: doubao-embedding-vision\n"
        "  siliconflow:\n"
        "    base_url: https://api.siliconflow.cn/v1\n"
        "    api_key_env: SILICONFLOW_API_KEY\n"
        "    default_model: BAAI/bge-m3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", path)
    # 重定向 persist_config 落盘路径，防污染真实 sgme.yaml
    cfg_path = tmp_path / "sgme_t.yaml"
    monkeypatch.setenv("SGME_CONFIG_PATH", str(cfg_path))
    cfg = {"llm": {"chains": {}}, "search": {"vector": {"provider": "volc-plan"}}}
    res = llm_ops.llm_embedding_set_active(cfg, "siliconflow")
    assert res.ok
    assert res.data["provider"] == "siliconflow"
    assert res.data["vector"]["base_url"] == "https://api.siliconflow.cn/v1"
    assert res.data["vector"]["api_key_env"] == "SILICONFLOW_API_KEY"
    assert res.data["vector"]["model"] == "BAAI/bge-m3"
    # 落盘读回
    assert "siliconflow" in sgme_config.load_embeddings_config()


def test_embedding_set_active_unknown_provider(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", path)
    cfg_path = tmp_path / "sgme_t.yaml"
    monkeypatch.setenv("SGME_CONFIG_PATH", str(cfg_path))
    with pytest.raises(llm_ops.OperationError) as ei:
        llm_ops.llm_embedding_set_active({}, "ghost")
    assert ei.value.error_code == "ERR_NOT_FOUND"


def test_llm_health_probe_injected():
    cfg = {"llm": {"chains": {"refinement": [
        {"provider": "deepseek", "base_url": "https://x/v1"},
        {"provider": "rule", "rule": "drop_batch"},
    ]}}}

    def fake_probe(info):
        return {"available": True, "latency_ms": 3}

    res = llm_ops.llm_health(cfg, probe=fake_probe)
    assert res.ok
    assert res.data["health"]["deepseek"] == {"available": True, "latency_ms": 3}
    # rule 不探测
    assert "rule" not in res.data["health"]


def test_probe_missing_base_url_returns_unavailable():
    res = llm_ops._probe_provider({"provider": "ghost", "base_url": ""})
    assert res["available"] is False
    assert "base_url" in res["error"]


def test_llm_health_probes_vector_capable_providers(prov_path):
    """健康探测范围并入 vector_capable 供应商（T-47：向量模型连通性）。"""
    prov_path.write_text(
        "providers:\n"
        "  siliconflow:\n"
        "    base_url: https://api.siliconflow.cn/v1\n"
        "    api_key_env: SILICONFLOW_API_KEY\n"
        "    vector_capable: true\n"
        "  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )
    cfg = {"llm": {"chains": {"refinement": [
        {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
        {"provider": "rule", "rule": "drop_batch"},
    ]}}}

    def fake_probe(info):
        return {"available": True, "latency_ms": 3, "provider": info.get("provider")}

    res = llm_ops.llm_health(cfg, probe=fake_probe)
    assert res.ok
    # 链中供应商探测
    assert "deepseek" in res.data["health"]
    # 未被链引用的 vector_capable 供应商也探测（向量模型连通性）
    assert "siliconflow" in res.data["health"]
    # rule 不探测
    assert "rule" not in res.data["health"]


# ---------- 供应商管理（写回 providers.yaml，tmp 重定向） ----------

@pytest.fixture
def prov_path(tmp_path, monkeypatch):
    """把 providers.yaml 重定向到临时文件，避免污染真实 config/providers.yaml。"""
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", path)
    return path


def test_provider_add_persists(prov_path):
    cfg = {"llm": {"chains": {"refinement": [
        {"provider": "deepseek", "base_url": "https://x/v1", "api_key_env": "K"},
        {"provider": "rule", "rule": "drop_batch"},
    ]}}}
    res = llm_ops.llm_provider_add(cfg, "ollama", {"base_url": "http://127.0.0.1:11434/v1", "api_key_env": "OLLAMA_KEY", "model": "qwen3"})
    assert res.ok
    assert res.data["provider"] == "ollama"
    # 落盘后可从文件读回
    providers = sgme_config.load_providers_config()
    assert providers["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert providers["ollama"]["api_key_env"] == "OLLAMA_KEY"


def test_provider_add_requires_api_key_env(prov_path):
    with pytest.raises(llm_ops.InvalidArgs) as ei:
        llm_ops.llm_provider_add({}, "ollama", {"base_url": "http://x/v1"})
    assert "api_key_env" in str(ei.value)


def test_provider_add_rejects_plaintext_key(prov_path):
    with pytest.raises(llm_ops.InvalidArgs) as ei:
        llm_ops.llm_provider_add({}, "ollama", {"base_url": "http://x/v1", "api_key_env": "K", "key": "sk-abc"})
    assert "明文密钥" in str(ei.value)


def test_provider_delete(prov_path):
    cfg = {"llm": {"chains": {"refinement": [
        {"provider": "deepseek", "base_url": "https://x/v1", "api_key_env": "K"},
    ]}}}
    llm_ops.llm_provider_add({}, "ollama", {"base_url": "http://x/v1", "api_key_env": "OK"})
    res = llm_ops.llm_provider_delete(cfg, "ollama")
    assert res.ok
    assert res.data["deleted"] is True
    assert "ollama" not in sgme_config.load_providers_config()


def test_provider_delete_blocked_when_referenced(prov_path):
    cfg = {"llm": {"chains": {"refinement": [
        {"provider": "deepseek", "base_url": "https://x/v1", "api_key_env": "K"},
    ]}}}
    # 先落盘 deepseek（被链引用），再删除 → 应被拒绝
    llm_ops.llm_provider_add({}, "deepseek", {"base_url": "https://x/v1", "api_key_env": "K"})
    with pytest.raises(llm_ops.InvalidArgs) as ei:
        llm_ops.llm_provider_delete(cfg, "deepseek")
    assert "引用" in str(ei.value)
    # 文件未被改动
    assert "deepseek" in sgme_config.load_providers_config()


def test_provider_delete_missing_returns_404(prov_path):
    with pytest.raises(llm_ops.OperationError) as ei:
        llm_ops.llm_provider_delete({}, "ghost")
    assert ei.value.error_code == "ERR_NOT_FOUND"


# ---------- 路由：POST / DELETE 供应商 ----------

def test_post_provider_route(prov_path, client):
    resp = client.post(
        "/v1/admin/llm/providers",
        json={"provider": "ollama", "payload": {"base_url": "http://127.0.0.1:11434/v1", "api_key_env": "OK", "model": "qwen3"}},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["provider"] == "ollama"


def test_delete_provider_route(prov_path, client):
    client.post(
        "/v1/admin/llm/providers",
        json={"provider": "ollama", "payload": {"base_url": "http://x/v1", "api_key_env": "OK"}},
        headers={"X-API-Key": "test-admin-key"},
    )
    resp = client.delete("/v1/admin/llm/providers/ollama", headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_provider_write_requires_admin(prov_path, client):
    resp = client.post(
        "/v1/admin/llm/providers",
        json={"provider": "ollama", "payload": {"base_url": "http://x/v1", "api_key_env": "OK"}},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 403


# ---------- 路由：PUT /v1/admin/llm/embedding/active（T-43 切换向量提供商） ----------

@pytest.fixture
def emb_path(tmp_path, monkeypatch):
    """把 providers.yaml embedding 段重定向到临时文件。"""
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers: {}\n"
        "embedding:\n"
        "  volc-plan:\n"
        "    base_url: https://ark/v3\n"
        "    api_key_env: VOLC_API_KEY\n"
        "    default_model: doubao-embedding-vision\n"
        "  siliconflow:\n"
        "    base_url: https://api.siliconflow.cn/v1\n"
        "    api_key_env: SILICONFLOW_API_KEY\n"
        "    default_model: BAAI/bge-m3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", path)
    return path


def test_put_embedding_active_route(tmp_path, monkeypatch, client):
    from sgme.server import app as app_mod
    # persist_config 落盘路径重定向，防污染真实 sgme.yaml
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_t.yaml"))
    resp = client.put(
        "/v1/admin/llm/embedding/active",
        json={"provider": "siliconflow"},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "siliconflow"
    assert data["vector"]["base_url"] == "https://api.siliconflow.cn/v1"


def test_put_embedding_active_requires_admin(tmp_path, monkeypatch, client):
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_t.yaml"))
    resp = client.put(
        "/v1/admin/llm/embedding/active",
        json={"provider": "siliconflow"},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 403


# ---------- T-44 统一供应商模型：vector_capable 落盘保留 embedding 段 ----------

def test_write_providers_config_preserves_embedding(tmp_path, monkeypatch):
    """写回 providers 段时保留顶层 embedding 段（T-44 修复）。"""
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers: {}\n"
        "embedding:\n"
        "  volc-plan:\n"
        "    base_url: https://ark/v3\n"
        "    api_key_env: VOLC_API_KEY\n"
        "    default_model: doubao-embedding-vision\n",
        encoding="utf-8",
    )
    sgme_config.write_providers_config(
        {"deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"}},
        path=path,
    )
    # providers 段更新成功
    providers = sgme_config.load_providers_config(path=path)
    assert "deepseek" in providers
    # embedding 段未被抹掉
    assert "volc-plan" in sgme_config.load_embeddings_config(path=path)


def test_provider_add_preserves_embedding_segment(prov_path):
    """通过操作层新增供应商，embedding 段保留（T-44）。"""
    prov_path.write_text(
        "providers: {}\n"
        "embedding:\n"
        "  volc-plan:\n"
        "    base_url: https://ark/v3\n"
        "    api_key_env: VOLC_API_KEY\n",
        encoding="utf-8",
    )
    res = llm_ops.llm_provider_add({}, "ollama", {"base_url": "http://127.0.0.1:11434/v1", "api_key_env": "OK"})
    assert res.ok
    assert "ollama" in sgme_config.load_providers_config(path=prov_path)
    assert "volc-plan" in sgme_config.load_embeddings_config(path=prov_path)


def test_status_marks_vector_capable_providers(tmp_path, monkeypatch):
    """providers 段标记 vector_capable=true 的供应商在 llm_status 中暴露（T-44）。"""
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    api_key_env: OLLAMA_KEY\n"
        "    vector_capable: true\n"
        "  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", path)
    cfg = {"llm": {"chains": {}}, "search": {"vector": {}}}
    res = llm_ops.llm_status(cfg)
    assert res.ok
    providers = res.data["providers"]
    assert providers["ollama"]["vector_capable"] is True
    assert providers["deepseek"]["vector_capable"] is False


def test_embedding_set_active_from_vector_capable_provider(tmp_path, monkeypatch):
    """统一供应商模型：从 vector_capable=true 供应商选向量模型（T-44）。"""
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  siliconflow:\n"
        "    base_url: https://api.siliconflow.cn/v1\n"
        "    api_key_env: SILICONFLOW_API_KEY\n"
        "    default_model: BAAI/bge-m3\n"
        "    models: [BAAI/bge-m3]\n"
        "    vector_capable: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", path)
    cfg_path = tmp_path / "sgme_t.yaml"
    monkeypatch.setenv("SGME_CONFIG_PATH", str(cfg_path))
    cfg = {"llm": {"chains": {}}, "search": {"vector": {}}}
    res = llm_ops.llm_embedding_set_active(cfg, "siliconflow")
    assert res.ok
    assert res.data["vector"]["model"] == "BAAI/bge-m3"
    assert res.data["vector"]["api_key_env"] == "SILICONFLOW_API_KEY"


# ---------- T-44 降级链更新：llm_chain_update + 路由 ----------

@pytest.fixture
def llm_path(tmp_path, monkeypatch):
    """把 llm.yaml 重定向到临时文件，避免污染真实 config/llm.yaml。"""
    path = tmp_path / "llm.yaml"
    path.write_text(
        "chains: {}\n"
        "rules:\n"
        "  timeout_s: 240\n"
        "  allowed_models:\n"
        "    deny_prefixes: [pro, reasoner, thinking]\n"
        "    deny_exact: [gemma-4-12b-qat]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_LLM_CONFIG", path)
    return path


def test_chain_update_persists_and_keeps_rules(tmp_path, monkeypatch, llm_path):
    """整体更新降级链：写回 llm.yaml，且保留 rules 段（T-44）。"""
    # providers 表：deepseek / lm-studio 存在
    prov = tmp_path / "providers.yaml"
    prov.write_text(
        "providers:\n"
        "  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n"
        "  lm-studio:\n"
        "    base_url: http://127.0.0.1:1014/v1\n"
        "    api_key_env: LM_STUDIO_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", prov)
    cfg = {"llm": {"chains": {}, "rules": {"timeout_s": 240}}}
    new_chains = {
        "refinement": [
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
            {"provider": "rule", "rule": "drop_batch"},
        ]
    }
    res = llm_ops.llm_chain_update(cfg, new_chains)
    assert res.ok
    assert res.data["chains"]["refinement"][0]["provider"] == "deepseek"
    # 落盘：chains 更新 + rules 保留
    raw = sgme_config._read_yaml(llm_path)
    assert raw["chains"]["refinement"][0]["provider"] == "deepseek"
    assert raw["rules"]["timeout_s"] == 240
    # 运行时 cfg 刷新
    assert cfg["llm"]["chains"]["refinement"][0]["provider"] == "deepseek"


def test_chain_update_strips_conn_fields_on_write(tmp_path, monkeypatch, llm_path):
    """写回时剥离连接字段：WebUI 传回的已注入节点不得落盘（2026-08-14 修复）。

    回归防护：内联旧值会覆盖 providers.yaml 新配置（「降级链与 provider 不一致」复发隐患）。
    """
    prov = tmp_path / "providers.yaml"
    prov.write_text(
        "providers:\n"
        "  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", prov)
    cfg = {"llm": {"chains": {}, "rules": {}}}
    # 模拟 WebUI 传回的运行时节点（load 已注入连接字段）
    new_chains = {
        "refinement": [
            {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "max_tokens": 16384,
                "base_url": "https://api.deepseek.com/v1",
                "api_key_env": "DEEPSEEK_API_KEY",
                "context_window": 1048576,
                "timeout_s": 120,
                "max_retries": 3,
                "health_endpoint": "/models",
                "health_interval_s": 60,
                "vector_capable": False,
            },
            {"provider": "rule", "rule": "drop_batch"},
        ]
    }
    res = llm_ops.llm_chain_update(cfg, new_chains)
    assert res.ok
    # 落盘：只留编排字段
    raw = sgme_config._read_yaml(llm_path)
    node = raw["chains"]["refinement"][0]
    assert node["provider"] == "deepseek"
    assert node["model"] == "deepseek-v4-flash"
    assert node["max_tokens"] == 16384
    for conn in ("base_url", "api_key_env", "context_window",
                 "timeout_s", "max_retries", "health_endpoint",
                 "health_interval_s", "vector_capable"):
        assert conn not in node, f"连接字段 {conn} 不应落盘"
    assert raw["chains"]["refinement"][1] == {"provider": "rule", "rule": "drop_batch"}
    # 运行时 cfg 仍持有完整连接字段（仅写盘剥离，运行不受影响）
    run_node = cfg["llm"]["chains"]["refinement"][0]
    assert run_node["base_url"] == "https://api.deepseek.com/v1"
    assert run_node["api_key_env"] == "DEEPSEEK_API_KEY"


def test_chain_update_rejects_unknown_provider(prov_path, llm_path):
    """降级链引用未知供应商 → 拒绝（T-44 校验）。"""
    cfg = {"llm": {"chains": {}, "rules": {}}}
    bad = {"refinement": [{"provider": "ghost", "model": "x"}]}
    with pytest.raises(llm_ops.InvalidArgs) as ei:
        llm_ops.llm_chain_update(cfg, bad)
    assert "未知供应商" in str(ei.value)


def test_chain_update_rejects_denied_model(tmp_path, monkeypatch, llm_path):
    """降级链命中白名单黑名单模型 → 拒绝（铁律 #9）。"""
    prov = tmp_path / "providers.yaml"
    prov.write_text(
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    api_key_env: OLLAMA_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", prov)
    cfg = {"llm": {"chains": {}, "rules": {"allowed_models": {
        "deny_prefixes": ["pro"], "deny_exact": ["gemma-4-12b-qat"],
    }}}}
    bad = {"refinement": [{"provider": "ollama", "model": "pro-xyz"}]}
    with pytest.raises(ValueError):
        llm_ops.llm_chain_update(cfg, bad)


def test_chain_update_cleanup_on_validation_error(tmp_path, monkeypatch, llm_path):
    """校验失败不应污染 llm.yaml（未知供应商被拒时文件保持原样）。"""
    prov = tmp_path / "providers.yaml"
    prov.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(sgme_config, "DEFAULT_PROVIDERS_CONFIG", prov)
    cfg = {"llm": {"chains": {}, "rules": {}}}
    bad = {"refinement": [{"provider": "ghost", "model": "x"}]}
    with pytest.raises(llm_ops.InvalidArgs):
        llm_ops.llm_chain_update(cfg, bad)
    # 文件未被写坏：chains 仍为空
    raw = sgme_config._read_yaml(llm_path)
    assert raw["chains"] == {}


def test_chain_update_requires_chains_object(llm_path):
    cfg = {"llm": {"chains": {}, "rules": {}}}
    with pytest.raises(llm_ops.InvalidArgs):
        llm_ops.llm_chain_update(cfg, ["not-a-dict"])


# ---------- 路由：PUT /v1/admin/llm/chains（T-44 降级链编辑） ----------

def test_put_chains_route(llm_path, prov_path, client):
    """PUT /v1/admin/llm/chains 整体更新降级链。"""
    # prov_path 是空 providers 表 → 需先落盘 deepseek
    prov_path.write_text(
        "providers:\n"
        "  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n",
        encoding="utf-8",
    )
    resp = client.put(
        "/v1/admin/llm/chains",
        json={"chains": {"refinement": [
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
            {"provider": "rule", "rule": "drop_batch"},
        ]}},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["chains"]["refinement"][0]["provider"] == "deepseek"
    # 落盘读回
    raw = sgme_config._read_yaml(llm_path)
    assert raw["chains"]["refinement"][0]["provider"] == "deepseek"


def test_put_chains_route_rejects_unknown_provider(llm_path, prov_path, client):
    resp = client.put(
        "/v1/admin/llm/chains",
        json={"chains": {"refinement": [{"provider": "ghost", "model": "x"}]}},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 400
    assert "未知供应商" in resp.json()["error"]["message"]


def test_put_chains_requires_admin(llm_path, prov_path, client):
    resp = client.put(
        "/v1/admin/llm/chains",
        json={"chains": {"refinement": [{"provider": "deepseek", "model": "x"}]}},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 403