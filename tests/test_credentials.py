"""凭据模块测试（EP-11 T-276）：叶子 / 数据层 / 操作层 / HTTP / MCP。

四类断言（方案 §十 验收点 2）：
① 错误信息与日志不含值；② 坏 key / 缺失 key 行为；③ 禁用态零影响（无新表、无新库文件）；
④ 软删边界（已删名写入 409 / restore 清 deleted_at / 幂等 / 读端过滤）。

样例一律低熵假值（B179 教训：样例即泄露，禁真实 key 形态）；全程 tmp_path 隔离
（conftest 已把 SGME_HOME 指向 tmp，测试不得触真实 data/）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme import credentials as cred
from sgme.data import credentials_dao as dao
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations import credentials as ops
from sgme.server.app import AgentKeyStore, create_app

FAKE_VALUE = "cred-test-value-0000"
FAKE_VALUE_2 = "cred-test-value-1111"
FAKE_VALUE_3 = "cred-test-value-2222"

ADMIN_KEY = "test-admin-key-cred"
AGENT_KEY = "test-agent-key-cred"


# ---------- 夹具 ----------


@pytest.fixture
def cred_key(monkeypatch):
    """合法 Fernet 主密钥（运行时生成，不落任何文件、不进代码）。"""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SGME_CRED_KEY", key)
    return key


@pytest.fixture
def creds_conn(tmp_path):
    conn = dao.connect_credentials(tmp_path / "data")
    yield conn
    conn.close()


@pytest.fixture
def cfg_on():
    """启用凭据模块的运行时配置（测试内显式钉住，不依赖出厂默认）。"""
    cfg = sgme_config.load_config()
    cfg["credentials"] = {"enabled": True}
    return cfg


@pytest.fixture
def conns(tmp_path, cfg_on):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg_on["dimensions"], cfg_on["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def make_client(cfg, conns, tmp_path, credentials_conn=None):
    """构建 TestClient（隔离密钥/连接；MCP 自托管由 conftest 全局禁用）。"""
    mem_conn, session_conn, wiki_conn = conns
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
        credentials_conn=credentials_conn,
    )
    return TestClient(app)


# ---------- 1) 叶子（crypto / key_name 校验） ----------


class TestLeaf:
    def test_key_name_validation(self):
        assert cred.validate_key_name("DEEPSEEK_API_KEY") == "DEEPSEEK_API_KEY"
        for bad in ("lowercase", "1ABC", "WITH-DASH", "", None, "A B"):
            with pytest.raises(ValueError):
                cred.validate_key_name(bad)
        with pytest.raises(ValueError):
            cred.validate_key_name("SGME_ADMIN_KEY")  # 保留名（引导凭据空间）

    def test_master_key_states(self, monkeypatch):
        monkeypatch.delenv("SGME_CRED_KEY", raising=False)
        assert cred.master_key_state(None) == {"ok": False, "state": "missing"}
        monkeypatch.setenv("SGME_CRED_KEY", "not-a-fernet-key")
        assert cred.master_key_state(None)["state"] == "invalid_format"
        monkeypatch.setenv("SGME_CRED_KEY", Fernet.generate_key().decode())
        assert cred.master_key_state(None) == {"ok": True, "state": "ok"}  # 空库视为通过
        cipher = cred.encrypt_value("x")
        monkeypatch.setenv("SGME_CRED_KEY", Fernet.generate_key().decode())
        assert cred.master_key_state(cipher) == {"ok": False, "state": "probe_failed"}

    def test_roundtrip_and_error_hygiene(self, cred_key, monkeypatch):
        cipher = cred.encrypt_value(FAKE_VALUE)
        assert cred.decrypt_value(cipher) == FAKE_VALUE
        monkeypatch.setenv("SGME_CRED_KEY", Fernet.generate_key().decode())
        with pytest.raises(cred.CredKeyError) as ei:
            cred.decrypt_value(cipher)
        assert FAKE_VALUE not in str(ei.value)


# ---------- 2) 数据层 ----------


class TestDao:
    def test_connect_schema(self, creds_conn):
        tables = {
            r[0]
            for r in creds_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"credentials", "credential_audit", "schema_versions"} <= tables
        v = creds_conn.execute("SELECT name FROM schema_versions").fetchone()
        assert v[0] == "credentials_v1"

    def test_bare_conn_tolerance(self, cred_key, tmp_path):
        """裸连接（无 row_factory）调查询 helper 不崩（sgme-development 三.21 回归）。"""
        conn = dao.connect_credentials(tmp_path / "data")
        assert ops.write_credential(
            conn, key_name="BARE_KEY", value=FAKE_VALUE, actor="t"
        ).ok
        conn.close()
        bare = sqlite3.connect(str(tmp_path / "data" / "credentials.db"))
        rows = dao.list_credentials(bare)
        assert rows and rows[0]["key_name"] == "BARE_KEY"
        bare.close()


# ---------- 3) 操作层（四类断言核心） ----------


class TestOperations:
    def test_write_refused_without_key(self, creds_conn, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        monkeypatch.delenv("SGME_CRED_KEY", raising=False)
        r = ops.write_credential(creds_conn, key_name="NOKEY_KEY", value=FAKE_VALUE, actor="t")
        assert (not r.ok) and r.error_code == "ERR_INTERNAL"
        assert "主密钥" in (r.message or "")
        assert FAKE_VALUE not in (r.message or "")  # ① 错误信息不含值
        assert FAKE_VALUE not in caplog.text  # ① 日志不含值

    def test_write_refused_on_probe_failed_and_bad_key_reads_clean(self, creds_conn, cred_key, monkeypatch):
        assert ops.write_credential(
            creds_conn, key_name="PROBE_KEY", value=FAKE_VALUE, actor="t"
        ).ok
        monkeypatch.setenv("SGME_CRED_KEY", Fernet.generate_key().decode())  # 换错 key
        r = ops.write_credential(creds_conn, key_name="PROBE2_KEY", value=FAKE_VALUE_2, actor="t")
        assert not r.ok and FAKE_VALUE_2 not in (r.message or "")
        r = ops.get_credential(creds_conn, "PROBE_KEY", actor="t")
        assert (not r.ok) and r.error_code == "ERR_INTERNAL"
        r = ops.export_credentials(creds_conn, actor="t")
        assert not r.ok

    def test_read_paths_log_no_value(self, creds_conn, cred_key, caplog):
        caplog.set_level(logging.DEBUG)
        assert ops.write_credential(
            creds_conn, key_name="LOG_KEY", value=FAKE_VALUE, actor="t"
        ).ok
        ops.list_credentials(creds_conn)
        ops.get_credential(creds_conn, "LOG_KEY", actor="t")
        ops.export_credentials(creds_conn, actor="t")
        assert FAKE_VALUE not in caplog.text
        fps = [
            r[0]
            for r in creds_conn.execute(
                "SELECT value_fp FROM credential_audit WHERE action='write' AND value_fp IS NOT NULL"
            ).fetchall()
        ]
        assert fps and all(len(fp) == 8 and FAKE_VALUE not in fp for fp in fps)

    def test_audit_actions_recorded(self, creds_conn, cred_key):
        ops.write_credential(creds_conn, key_name="AUDIT_KEY", value=FAKE_VALUE, actor="t")
        ops.list_credentials(creds_conn)  # list 不记账
        ops.get_credential(creds_conn, "AUDIT_KEY", actor="t")
        ops.export_credentials(creds_conn, actor="t")
        ops.delete_credential(creds_conn, key_name="AUDIT_KEY", actor="t")
        ops.restore_credential(creds_conn, key_name="AUDIT_KEY", actor="t")
        actions = [
            r[0]
            for r in creds_conn.execute("SELECT action FROM credential_audit").fetchall()
        ]
        for want in ("write", "read", "export", "delete", "restore"):
            assert want in actions
        # list 不记账：逐次调用不产生新行
        before = len(actions)
        ops.list_credentials(creds_conn)
        after = creds_conn.execute("SELECT COUNT(*) FROM credential_audit").fetchone()[0]
        assert after == before

    def test_export_semantics(self, creds_conn, cred_key):
        ops.write_credential(creds_conn, key_name="LLM_A_KEY", value=FAKE_VALUE, actor="t")
        ops.write_credential(creds_conn, key_name="LLM_B_KEY", value=FAKE_VALUE_2, actor="t")
        ops.write_credential(creds_conn, key_name="OTHER_KEY", value=FAKE_VALUE_3, actor="t")
        r = ops.export_credentials(creds_conn, prefix="LLM_", actor="t")
        assert r.ok and r.data["credentials"] == {
            "LLM_A_KEY": FAKE_VALUE,
            "LLM_B_KEY": FAKE_VALUE_2,
        }
        r = ops.export_credentials(creds_conn, only="OTHER_KEY,LLM_A_KEY", actor="t")
        assert r.ok and set(r.data["credentials"]) == {"OTHER_KEY", "LLM_A_KEY"}
        r = ops.export_credentials(creds_conn, only="NOT_EXIST", actor="t")
        assert (not r.ok) and r.error_code == "ERR_NOT_FOUND"
        r = ops.export_credentials(creds_conn, only="A", prefix="B", actor="t")
        assert (not r.ok) and r.error_code == "ERR_INVALID_ARGS"

    def test_soft_delete_boundaries(self, creds_conn, cred_key):
        assert ops.write_credential(
            creds_conn, key_name="DEL_KEY", value=FAKE_VALUE, actor="t"
        ).ok
        # 删除 → 读端过滤 + 已删名写入 409
        assert ops.delete_credential(creds_conn, key_name="DEL_KEY", actor="t").data["changed"] is True
        r = ops.write_credential(creds_conn, key_name="DEL_KEY", value=FAKE_VALUE_2, actor="t")
        assert (not r.ok) and r.error_code == "ERR_CONFLICT"
        assert ops.get_credential(creds_conn, "DEL_KEY", actor="t").error_code == "ERR_NOT_FOUND"
        assert ops.list_credentials(creds_conn).data["count"] == 0
        assert "DEL_KEY" not in ops.export_credentials(creds_conn, actor="t").data["credentials"]
        # 幂等（重复调用 no-op 成功）
        assert ops.delete_credential(creds_conn, key_name="DEL_KEY", actor="t").data["changed"] is False
        # restore 清 deleted_at；幂等
        r = ops.restore_credential(creds_conn, key_name="DEL_KEY", actor="t")
        assert r.ok and r.data["changed"] is True
        row = creds_conn.execute(
            "SELECT status, deleted_at FROM credentials WHERE key_name='DEL_KEY'"
        ).fetchone()
        assert row["status"] == "active" and row["deleted_at"] is None
        assert ops.restore_credential(creds_conn, key_name="DEL_KEY", actor="t").data["changed"] is False
        assert ops.get_credential(creds_conn, "DEL_KEY", actor="t").data["value"] == FAKE_VALUE

    def test_delete_restore_do_not_need_key(self, creds_conn, cred_key, monkeypatch):
        """软删/恢复不触碰密文 → 主密钥缺失时仍可用（修复窗口的必要操作）。"""
        assert ops.write_credential(
            creds_conn, key_name="FIX_KEY", value=FAKE_VALUE, actor="t"
        ).ok
        monkeypatch.delenv("SGME_CRED_KEY", raising=False)
        assert ops.delete_credential(creds_conn, key_name="FIX_KEY", actor="t").ok is True
        assert ops.restore_credential(creds_conn, key_name="FIX_KEY", actor="t").ok is True

    def test_unknown_key_404s(self, creds_conn, cred_key):
        assert ops.get_credential(creds_conn, "NOPE_KEY", actor="t").error_code == "ERR_NOT_FOUND"
        assert ops.delete_credential(creds_conn, key_name="NOPE_KEY", actor="t").error_code == "ERR_NOT_FOUND"
        assert ops.restore_credential(creds_conn, key_name="NOPE_KEY", actor="t").error_code == "ERR_NOT_FOUND"


# ---------- 4) HTTP 端点 ----------


class TestHttp:
    @pytest.fixture
    def client_on(self, cfg_on, conns, creds_conn, cred_key, tmp_path):
        return make_client(cfg_on, conns, tmp_path, credentials_conn=creds_conn)

    def test_agent_reads_admin_writes(self, client_on):
        h_agent = {"X-API-Key": AGENT_KEY}
        h_admin = {"X-API-Key": ADMIN_KEY}
        r = client_on.post(
            "/v1/credentials",
            json={"key_name": "HTTP_KEY", "value": FAKE_VALUE, "description": "d"},
            headers=h_admin,
        )
        assert r.status_code == 200 and r.json()["created"] is True
        # 列表无值
        lst = client_on.get("/v1/credentials", headers=h_agent).json()
        assert lst["count"] == 1 and "value" not in lst["credentials"][0]
        # 取单值
        g = client_on.get("/v1/credentials/HTTP_KEY", headers=h_agent)
        assert g.status_code == 200 and g.json()["value"] == FAKE_VALUE
        # 写侧 agent 越权
        assert client_on.post(
            "/v1/credentials", json={"key_name": "X_KEY", "value": "v"}, headers=h_agent
        ).status_code == 403
        assert client_on.delete("/v1/credentials/HTTP_KEY", headers=h_agent).status_code == 403
        # export 静态路径先于 /{key}（若被吞会当键名 404）
        e = client_on.get("/v1/credentials/export", headers=h_agent)
        assert e.status_code == 200 and e.json()["credentials"] == {"HTTP_KEY": FAKE_VALUE}
        # 未知键 404
        assert client_on.get("/v1/credentials/NOPE_KEY", headers=h_agent).status_code == 404
        # 删 → 409 → 恢复
        assert client_on.delete("/v1/credentials/HTTP_KEY", headers=h_admin).status_code == 200
        c = client_on.post(
            "/v1/credentials", json={"key_name": "HTTP_KEY", "value": "v"}, headers=h_admin
        )
        assert c.status_code == 409
        assert client_on.post("/v1/credentials/HTTP_KEY/restore", headers=h_admin).status_code == 200

    def test_disabled_module_routes_absent(self, conns, tmp_path):
        cfg = sgme_config.load_config()  # 出厂默认：disabled
        assert (cfg.get("credentials") or {}).get("enabled") is False
        c = make_client(cfg, conns, tmp_path, credentials_conn=None)
        r = c.get("/v1/credentials", headers={"X-API-Key": AGENT_KEY})
        assert r.status_code in (404, 405)  # SPA catch-all 形态差异，环境无关断言
        r = c.post(
            "/v1/credentials", json={"key_name": "A_KEY", "value": "v"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code in (404, 405)

    def test_disabled_no_db_file_enabled_creates(self, cfg_on, conns, tmp_path):
        """③ 禁用态零影响（无新库文件）；启用态按 DATA_DIR 自动建库（隔离 tmp）。"""
        cfg_off = dict(cfg_on)
        cfg_off["credentials"] = {"enabled": False}
        make_client(cfg_off, conns, tmp_path, credentials_conn=None)
        assert not list(tmp_path.rglob("credentials.db"))
        make_client(cfg_on, conns, tmp_path, credentials_conn=None)
        assert (sgme_config.DATA_DIR / "credentials.db").exists()


# ---------- 5) MCP ----------


class TestMcp:
    def _bind(self, cfg, creds_conn, tmp_path):
        store = AgentKeyStore(
            admin_key=ADMIN_KEY, agent_key=AGENT_KEY, store_path=tmp_path / "ak.json"
        )
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data_mcp")
        bind_app_state({
            "cfg": cfg,
            "mem_conn": mem_conn,
            "session_conn": session_conn,
            "wiki_conn": wiki_conn,
            "key_store": store,
            "credentials_conn": creds_conn,
        })
        return mem_conn, session_conn, wiki_conn

    @staticmethod
    def _call(server, name, args):
        raw = asyncio.run(server.call_tool(name, args))
        results, _meta = raw if isinstance(raw, tuple) else (raw, None)
        return json.loads("".join(c.text for c in results if getattr(c, "text", None)))

    def test_mcp_tools(self, cfg_on, creds_conn, cred_key, tmp_path):
        conns = self._bind(cfg_on, creds_conn, tmp_path)
        assert ops.write_credential(
            creds_conn, key_name="MCP_KEY", value=FAKE_VALUE, actor="t"
        ).ok
        server = build_mcp_server()
        data = self._call(server, "credential_list", {})
        assert data["count"] == 1 and data["credentials"][0]["key_name"] == "MCP_KEY"
        data = self._call(server, "credential_get", {"name": "MCP_KEY"})
        assert data["value"] == FAKE_VALUE
        data = self._call(server, "credential_get", {"name": "NOPE_KEY"})
        assert "error" in data
        for c in conns:
            c.close()

    def test_mcp_disabled_friendly_error(self, creds_conn, tmp_path):
        cfg = sgme_config.load_config()  # 出厂默认：disabled
        conns = self._bind(cfg, creds_conn, tmp_path)
        server = build_mcp_server()
        data = self._call(server, "credential_list", {})
        assert "未启用" in json.dumps(data, ensure_ascii=False)
        for c in conns:
            c.close()
