"""凭据模块测试（EP-11 T-276）：叶子 / 数据层 / 操作层 / HTTP / MCP。

四类断言（方案 §十 验收点 2）：
① 错误信息与日志不含值；② 坏 key / 缺失 key 行为；③ 禁用态零影响（无新表、无新库文件）；
④ 软删边界（已删名写入 409 / restore 清 deleted_at / 幂等 / 读端过滤）。

样例一律低熵假值（B179 教训：样例即泄露，禁真实 key 形态）；全程 tmp_path 隔离
（conftest 已把 SGME_HOME 指向 tmp，测试不得触真实 data/）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
        # D-37 v1.2：文件表 credential_files 与文本面同库（密文 BLOB）
        assert {
            "credentials", "credential_audit", "credential_files", "schema_versions"
        } <= tables
        row = creds_conn.execute(
            "SELECT version, name FROM schema_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert row[0] == dao.SCHEMA_VERSION == 3
        assert row[1] == "credentials_v2"  # 结构代际名（v3 = 带文件表的第二代）

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

    def test_mcp_scope_filtering(self, cfg_on, creds_conn, cred_key, tmp_path):
        """D-37 v1.1：MCP 直调无身份 → 只能读 scope=all，admin-only 不可见/不可读。"""
        conns = self._bind(cfg_on, creds_conn, tmp_path)
        ops.write_credential(creds_conn, key_name="MCP_ADMIN", value=FAKE_VALUE, scope="admin", actor="t")
        ops.write_credential(creds_conn, key_name="MCP_ALL", value=FAKE_VALUE_2, actor="t")
        server = build_mcp_server()
        data = self._call(server, "credential_list", {})
        assert {c["key_name"] for c in data["credentials"]} == {"MCP_ALL"}
        data = self._call(server, "credential_get", {"name": "MCP_ADMIN"})
        assert "error" in data
        data = self._call(server, "credential_get", {"name": "MCP_ALL"})
        assert data["value"] == FAKE_VALUE_2
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

    def test_mcp_file_metadata_text_only(self, cfg_on, creds_conn, cred_key, tmp_path):
        """D-37 v1.2：MCP 文本面只给文件元数据；credential_get 对 file 键返回 error。"""
        conns = self._bind(cfg_on, creds_conn, tmp_path)
        assert ops.put_file(
            creds_conn, key_name="MCP_FILE", filename="mcp.bin",
            data=b"mcp-file-bytes", actor="t",
        ).ok
        server = build_mcp_server()
        data = self._call(server, "credential_list", {})
        item = [c for c in data["credentials"] if c["key_name"] == "MCP_FILE"][0]
        assert item["filename"] == "mcp.bin"
        assert item["file_size"] == len(b"mcp-file-bytes")
        assert item["file_sha256"] == hashlib.sha256(b"mcp-file-bytes").hexdigest()
        assert "value" not in item and "data" not in item
        got = self._call(server, "credential_get", {"name": "MCP_FILE"})
        assert "error" in got and "/file" in got["error"]
        # 二进制不进文本面：工具集里不存在任何 file 读写工具
        names = {t.name for t in asyncio.run(server.list_tools())}
        assert not {n for n in names if "credential_file" in n or n.endswith("_file")}
        for c in conns:
            c.close()


# ---------- 6) D-37 v1.1：scope / 过期 / last_used / merge ----------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _future(days: int = 1) -> str:
    return _iso(datetime.now(UTC) + timedelta(days=days))


class TestV11Extension:
    def test_normalize_helpers(self):
        assert ops.normalize_scope(" agents:dsh, hermes,dsh ") == "agents:dsh,hermes"
        assert ops.normalize_scope("all") == "all"
        assert ops.normalize_scope("admin") == "admin"
        assert ops.normalize_scope("") is None
        assert ops.normalize_scope(None) is None
        for bad in ("weird", "agents:", "agents:a b"):
            with pytest.raises(ValueError):
                ops.normalize_scope(bad)
        assert ops.normalize_cred_type("token") == "token"
        # D-37 v1.2：file 是合法类型（但只能经文件端点写入，见 TestFileStorage）
        assert ops.normalize_cred_type("file") == ops.CRED_TYPE_FILE
        assert ops.normalize_cred_type("") is None
        with pytest.raises(ValueError):
            ops.normalize_cred_type("weird")
        future = _future(2)
        assert ops.normalize_expires_at(future) == future
        assert ops.normalize_expires_at("") is None
        with pytest.raises(ValueError):
            ops.normalize_expires_at("2000-01-01T00:00:00Z")
        with pytest.raises(ValueError):
            ops.normalize_expires_at("not-a-time")
        assert ops.is_expired(None) is False
        assert ops.is_expired("2000-01-01T00:00:00Z") is True
        assert ops.is_expired("garbage") is True  # fail-closed

    def test_scope_allows_matrix(self):
        assert ops.scope_allows(None, "dsh", False) is True
        assert ops.scope_allows("all", "dsh", False) is True
        assert ops.scope_allows("admin", "dsh", False) is False
        assert ops.scope_allows("admin", "dsh", True) is True
        assert ops.scope_allows("agents:dsh,hermes", "hermes", False) is True
        assert ops.scope_allows("agents:dsh,hermes", "other", False) is False
        assert ops.scope_allows("weird", "dsh", False) is False  # fail-closed
        assert ops.scope_allows("agents:dsh", None, False) is False

    def test_columns_roundtrip_and_last_used(self, creds_conn, cred_key):
        exp = _future(2)
        r = ops.write_credential(
            creds_conn, key_name="EXT_KEY", value=FAKE_VALUE,
            scope="agents:dsh,hermes", cred_type="token", expires_at=exp, actor="t",
        )
        assert r.ok and r.data["scope"] == "agents:dsh,hermes"
        row = dao.get_credential(creds_conn, "EXT_KEY")
        assert row["scope"] == "agents:dsh,hermes"
        assert row["cred_type"] == "token"
        assert row["expires_at"] == exp
        assert row["last_used_at"] is None
        listed = {c["key_name"]: c for c in dao.list_credentials(creds_conn)}
        assert listed["EXT_KEY"]["scope"] == "agents:dsh,hermes"
        assert listed["EXT_KEY"]["last_used_at"] is None
        assert ops.get_credential(
            creds_conn, "EXT_KEY", actor="t", agent_id="dsh", is_admin=False
        ).ok
        assert dao.get_credential(creds_conn, "EXT_KEY")["last_used_at"] is not None
        # 列表不更新 last_used
        before = dao.get_credential(creds_conn, "EXT_KEY")["last_used_at"]
        ops.list_credentials(creds_conn, agent_id="dsh", is_admin=False)
        assert dao.get_credential(creds_conn, "EXT_KEY")["last_used_at"] == before

    def test_scope_filtering_and_denied_audit(self, creds_conn, cred_key):
        ops.write_credential(creds_conn, key_name="ADMIN_ONLY", value=FAKE_VALUE, scope="admin", actor="t")
        ops.write_credential(creds_conn, key_name="DSH_ONLY", value=FAKE_VALUE_2, scope="agents:dsh", actor="t")
        ops.write_credential(creds_conn, key_name="PUBLIC_KEY", value=FAKE_VALUE_3, actor="t")
        dsh = ops.list_credentials(creds_conn, agent_id="dsh", is_admin=False)
        assert {c["key_name"] for c in dsh.data["credentials"]} == {"DSH_ONLY", "PUBLIC_KEY"}
        hermes = ops.list_credentials(creds_conn, agent_id="hermes", is_admin=False)
        assert {c["key_name"] for c in hermes.data["credentials"]} == {"PUBLIC_KEY"}
        assert ops.list_credentials(creds_conn, is_admin=True).data["count"] == 3
        r = ops.get_credential(creds_conn, "ADMIN_ONLY", actor="dsh(agent,mcp)", agent_id="dsh", is_admin=False)
        assert (not r.ok) and r.error_code == "ERR_FORBIDDEN"
        assert ops.get_credential(creds_conn, "DSH_ONLY", actor="x", agent_id="dsh", is_admin=False).ok
        r = ops.get_credential(creds_conn, "DSH_ONLY", actor="x", agent_id="hermes", is_admin=False)
        assert r.error_code == "ERR_FORBIDDEN"
        assert ops.get_credential(creds_conn, "ADMIN_ONLY", actor="t", is_admin=True).ok
        denied = creds_conn.execute(
            "SELECT COUNT(*) FROM credential_audit WHERE action='denied'"
        ).fetchone()[0]
        assert denied == 2

    def test_expiry_semantics(self, creds_conn, cred_key, monkeypatch):
        base = datetime(2030, 1, 1, tzinfo=UTC)
        monkeypatch.setattr(ops, "_now", lambda: base)
        exp = "2030-01-02T00:00:00Z"
        assert ops.write_credential(creds_conn, key_name="EXP_KEY", value=FAKE_VALUE, expires_at=exp, actor="t").ok
        assert ops.get_credential(creds_conn, "EXP_KEY", actor="t", agent_id="x", is_admin=False).ok
        monkeypatch.setattr(ops, "_now", lambda: base + timedelta(days=2))
        agent = ops.list_credentials(creds_conn, agent_id="x", is_admin=False)
        assert all(c["key_name"] != "EXP_KEY" for c in agent.data["credentials"])
        item = [
            c for c in ops.list_credentials(creds_conn, is_admin=True).data["credentials"]
            if c["key_name"] == "EXP_KEY"
        ][0]
        assert item["expired"] is True
        r = ops.get_credential(creds_conn, "EXP_KEY", actor="t", is_admin=True)
        assert r.error_code == "ERR_FORBIDDEN" and "过期" in (r.message or "")
        e = ops.export_credentials(creds_conn, actor="t", is_admin=True)
        assert {s["key_name"]: s["reason"] for s in e.data["skipped"]}["EXP_KEY"] == "expired"
        assert ops.export_credentials(creds_conn, only="EXP_KEY", actor="t", is_admin=True).error_code == "ERR_FORBIDDEN"

    def test_merge_semantics_and_metadata_only(self, creds_conn, cred_key, monkeypatch):
        exp = _future(2)
        ops.write_credential(
            creds_conn, key_name="MERGE_KEY", value=FAKE_VALUE, description="d",
            scope="admin", cred_type="token", expires_at=exp, actor="t",
        )
        assert ops.write_credential(creds_conn, key_name="MERGE_KEY", value=FAKE_VALUE_2, actor="t").ok
        row = dao.get_credential(creds_conn, "MERGE_KEY")
        assert row["description"] == "d" and row["scope"] == "admin"
        assert row["cred_type"] == "token" and row["expires_at"] == exp
        assert cred.decrypt_value(row["value_cipher"]) == FAKE_VALUE_2
        # 元数据-only：不触碰密文 → 主密钥缺失也可用
        monkeypatch.delenv("SGME_CRED_KEY", raising=False)
        assert ops.write_credential(creds_conn, key_name="MERGE_KEY", description="新备注", actor="t").ok
        assert dao.get_credential(creds_conn, "MERGE_KEY")["description"] == "新备注"
        # 空串清空
        assert ops.write_credential(
            creds_conn, key_name="MERGE_KEY", description="", scope="",
            cred_type="", expires_at="", actor="t",
        ).ok
        row = dao.get_credential(creds_conn, "MERGE_KEY")
        assert row["description"] is None and row["scope"] is None
        assert row["cred_type"] is None and row["expires_at"] is None
        # 新键必须给 value
        assert ops.write_credential(creds_conn, key_name="NOVAL_KEY", actor="t").error_code == "ERR_INVALID_ARGS"

    def test_export_skipped_structure(self, creds_conn, cred_key):
        ops.write_credential(creds_conn, key_name="PUB_EXP", value=FAKE_VALUE, actor="t")
        ops.write_credential(creds_conn, key_name="ADMIN_EXP", value=FAKE_VALUE_2, scope="admin", actor="t")
        r = ops.export_credentials(creds_conn, actor="x", agent_id="dsh", is_admin=False)
        assert r.ok and set(r.data["credentials"]) == {"PUB_EXP"}
        assert r.data["skipped"] == [{"key_name": "ADMIN_EXP", "reason": "scope"}]
        assert r.data["count"] == 1


class TestHttpV11:
    @pytest.fixture
    def client_on(self, cfg_on, conns, creds_conn, cred_key, tmp_path):
        return make_client(cfg_on, conns, tmp_path, credentials_conn=creds_conn)

    def test_fields_scope_and_update(self, client_on):
        h_admin = {"X-API-Key": ADMIN_KEY}
        h_agent = {"X-API-Key": AGENT_KEY}
        exp = _future(3)
        r = client_on.post(
            "/v1/credentials",
            json={
                "key_name": "HTTP_ADMIN", "value": FAKE_VALUE, "description": "d",
                "scope": "admin", "cred_type": "api_key", "expires_at": exp,
            },
            headers=h_admin,
        )
        assert r.status_code == 200 and r.json()["scope"] == "admin"
        lst = client_on.get("/v1/credentials", headers=h_agent).json()
        assert all(c["key_name"] != "HTTP_ADMIN" for c in lst["credentials"])
        item = [
            c for c in client_on.get("/v1/credentials", headers=h_admin).json()["credentials"]
            if c["key_name"] == "HTTP_ADMIN"
        ][0]
        assert item["scope"] == "admin" and item["cred_type"] == "api_key"
        assert item["expires_at"] == exp and item["expired"] is False
        assert client_on.get("/v1/credentials/HTTP_ADMIN", headers=h_agent).status_code == 403
        assert client_on.get("/v1/credentials/HTTP_ADMIN", headers=h_admin).status_code == 200
        # 元数据更新可省略 value（value=None）
        r = client_on.post(
            "/v1/credentials", json={"key_name": "HTTP_ADMIN", "scope": "all"}, headers=h_admin
        )
        assert r.status_code == 200 and r.json()["scope"] == "all"
        assert client_on.get("/v1/credentials/HTTP_ADMIN", headers=h_agent).status_code == 200
        # 非法值 / 过去时间 / 新键无值
        assert client_on.post(
            "/v1/credentials", json={"key_name": "HTTP_ADMIN", "scope": "weird"}, headers=h_admin
        ).status_code == 400
        assert client_on.post(
            "/v1/credentials",
            json={"key_name": "HTTP_ADMIN", "expires_at": "2000-01-01T00:00:00Z"},
            headers=h_admin,
        ).status_code == 400
        assert client_on.post(
            "/v1/credentials", json={"key_name": "HTTP_NOVAL"}, headers=h_admin
        ).status_code == 400
        # agents 白名单：AGENT_KEY 反查为 default
        r = client_on.post(
            "/v1/credentials",
            json={"key_name": "HTTP_AGENTS", "value": "v", "scope": "agents:default"},
            headers=h_admin,
        )
        assert r.status_code == 200
        assert client_on.get("/v1/credentials/HTTP_AGENTS", headers=h_agent).status_code == 200

    def test_expired_http(self, client_on, monkeypatch):
        h_admin = {"X-API-Key": ADMIN_KEY}
        h_agent = {"X-API-Key": AGENT_KEY}
        exp = _iso(datetime.now(UTC) + timedelta(hours=1))
        assert client_on.post(
            "/v1/credentials",
            json={"key_name": "HTTP_EXP", "value": FAKE_VALUE, "expires_at": exp},
            headers=h_admin,
        ).status_code == 200
        monkeypatch.setattr(ops, "_now", lambda: datetime.now(UTC) + timedelta(hours=2))
        assert all(
            c["key_name"] != "HTTP_EXP"
            for c in client_on.get("/v1/credentials", headers=h_agent).json()["credentials"]
        )
        item = [
            c for c in client_on.get("/v1/credentials", headers=h_admin).json()["credentials"]
            if c["key_name"] == "HTTP_EXP"
        ][0]
        assert item["expired"] is True
        assert client_on.get("/v1/credentials/HTTP_EXP", headers=h_admin).status_code == 403
        assert client_on.get("/v1/credentials/HTTP_EXP", headers=h_agent).status_code == 403
        assert client_on.get("/v1/credentials/export?only=HTTP_EXP", headers=h_admin).status_code == 403

# ---------- 7) D-37 v1.2：文件 / 二进制「重要数据」 ----------

# 含 NUL 与非 UTF-8 字节：证明二进制通道不是「文本 base64」伪装
FAKE_FILE_BYTES = b"\x00\x01file-bytes-\xff-plain"
FILE_SHA = hashlib.sha256(FAKE_FILE_BYTES).hexdigest()


class TestFileLeaf:
    def test_bytes_roundtrip_and_error_hygiene(self, cred_key, monkeypatch):
        payload = b"\x00\x01binary\xff"
        token = cred.encrypt_bytes(payload)
        assert isinstance(token, bytes) and payload not in token
        assert cred.decrypt_bytes(token) == payload
        # 换错 key → CredKeyError，且消息不含任何明文字节
        monkeypatch.setenv("SGME_CRED_KEY", Fernet.generate_key().decode())
        with pytest.raises(cred.CredKeyError) as ei:
            cred.decrypt_bytes(token)
        assert "binary" not in str(ei.value)
        # 主密钥缺失 → 加密/解密都拒绝（绝不自动生成 key）
        monkeypatch.delenv("SGME_CRED_KEY", raising=False)
        with pytest.raises(cred.CredKeyError):
            cred.encrypt_bytes(payload)
        with pytest.raises(cred.CredKeyError):
            cred.decrypt_bytes(token)
        # 非法输入类型 → CredKeyError（不是 TypeError 泄漏到入口层）
        with pytest.raises(cred.CredKeyError):
            cred.encrypt_bytes("text-not-bytes")  # type: ignore[arg-type]


class TestFileDao:
    def test_file_table_crud_and_meta(self, creds_conn, cred_key):
        payload = b"hello-file"
        assert dao.upsert_file(
            creds_conn, key_name="DAO_FILE", filename="a.bin", size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            data_cipher=cred.encrypt_bytes(payload), actor="t1", at="t1",
        ) is True
        row = dao.get_file(creds_conn, "DAO_FILE")
        assert row["filename"] == "a.bin" and row["size"] == len(payload)
        assert isinstance(row["data_cipher"], bytes)
        assert cred.decrypt_bytes(row["data_cipher"]) == payload
        assert row["created_at"] == "t1" and row["updated_at"] == "t1"
        # 替换：created_at 保持首次写入值
        assert dao.upsert_file(
            creds_conn, key_name="DAO_FILE", filename="b.bin", size=3, sha256="s2",
            data_cipher=cred.encrypt_bytes(b"abc"), actor="t2", at="t2",
        ) is False
        row2 = dao.get_file(creds_conn, "DAO_FILE")
        assert (row2["filename"], row2["created_at"], row2["updated_at"], row2["updated_by"]) == (
            "b.bin", "t1", "t2", "t2",
        )
        # 元数据视图：只给 filename/size/sha256，绝不带 data_cipher
        meta = dao.list_file_meta(creds_conn)
        assert set(meta) == {"DAO_FILE"}
        assert "data_cipher" not in meta["DAO_FILE"]
        assert meta["DAO_FILE"]["sha256"] == "s2"
        assert dao.get_file(creds_conn, "NOPE_FILE") is None

    def test_legacy_db_gets_file_table_and_v2_row(self, tmp_path):
        """老库（v1 形态：无文件表、登记 v2/credentials_v1）连库即补建表 + 追加 v2 行（幂等）。"""
        data_dir = tmp_path / "legacy" / "data"
        data_dir.mkdir(parents=True)
        old = sqlite3.connect(str(data_dir / "credentials.db"))
        old.execute(
            "CREATE TABLE credentials (id INTEGER PRIMARY KEY, key_name TEXT UNIQUE NOT NULL, "
            "value_cipher TEXT NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'active', "
            "deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "updated_by TEXT NOT NULL)"
        )
        old.execute(
            "CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at TEXT NOT NULL)"
        )
        old.execute("INSERT INTO schema_versions VALUES (2,'credentials_v1','2026-11-04T00:00:00Z')")
        old.commit()
        old.close()

        conn = dao.connect_credentials(data_dir)
        tables = {x[0] for x in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "credential_files" in tables  # 老库连库即建表
        rows = [(x[0], x[1]) for x in conn.execute(
            "SELECT version, name FROM schema_versions ORDER BY version"
        ).fetchall()]
        assert rows == [(2, "credentials_v1"), (3, "credentials_v2")]
        conn.close()

        # 幂等：再连一次不重复插 v2 行
        conn2 = dao.connect_credentials(data_dir)
        assert conn2.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE name='credentials_v2'"
        ).fetchone()[0] == 1
        conn2.close()



class TestFileOperations:
    def test_put_and_read_roundtrip(self, creds_conn, cred_key):
        r = ops.put_file(
            creds_conn, key_name="FILE_KEY", filename="报告.bin",
            data=FAKE_FILE_BYTES, description="d", actor="t",
        )
        assert r.ok, r.message
        assert set(r.data) == {
            "ok", "key_name", "filename", "size", "sha256",
            "updated_at", "scope", "cred_type", "expires_at",
        }
        assert r.data["cred_type"] == "file" and r.data["size"] == len(FAKE_FILE_BYTES)
        assert r.data["sha256"] == FILE_SHA and r.data["scope"] is None
        got = ops.read_file(creds_conn, "FILE_KEY", actor="t")
        assert got.ok and got.data["data"] == FAKE_FILE_BYTES
        assert set(got.data) == {"key_name", "filename", "size", "sha256", "data"}
        # credentials 行：cred_type=file + 占位符密文（真数据在 credential_files）
        row = dao.get_credential(creds_conn, "FILE_KEY")
        assert row["cred_type"] == ops.CRED_TYPE_FILE
        assert cred.decrypt_value(row["value_cipher"]) == ops.CRED_FILE_PLACEHOLDER
        assert row["last_used_at"] is not None  # 读成功更新 last_used_at
        # 审计：file_write / file_read 分账；指纹只留 sha256[:8]
        actions = [x[0] for x in creds_conn.execute(
            "SELECT action FROM credential_audit ORDER BY id"
        ).fetchall()]
        assert "file_write" in actions and "file_read" in actions
        fp = creds_conn.execute(
            "SELECT value_fp FROM credential_audit WHERE action='file_write'"
        ).fetchone()[0]
        assert fp == FILE_SHA[:8] and FAKE_FILE_BYTES.decode("latin-1") not in str(fp)

    def test_at_rest_has_no_plaintext(self, creds_conn, cred_key, tmp_path):
        marker = b"PLAINTEXT-MARKER-0123456789"
        assert ops.put_file(
            creds_conn, key_name="AT_REST", filename="x.bin", data=marker, actor="t"
        ).ok
        creds_conn.commit()
        blob = creds_conn.execute(
            "SELECT data_cipher FROM credential_files WHERE key_name='AT_REST'"
        ).fetchone()[0]
        value_cipher = creds_conn.execute(
            "SELECT value_cipher FROM credentials WHERE key_name='AT_REST'"
        ).fetchone()[0]
        assert marker not in blob  # 列内即密文
        assert marker.decode() not in value_cipher
        assert cred.decrypt_bytes(blob) == marker
        # 库文件（含 WAL）也不含明文
        db_files = list(Path(tmp_path).rglob("credentials.db*"))
        assert db_files
        for path in db_files:
            assert marker not in path.read_bytes(), f"明文落盘：{path}"

    def test_limits_and_master_key_gate(self, creds_conn, cred_key, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        r = ops.put_file(
            creds_conn, key_name="BIG_KEY", filename="b.bin",
            data=b"x" * 10, actor="t", max_bytes=4,
        )
        assert r.error_code == "ERR_TOO_LARGE" and "10" in (r.message or "")
        assert ops.put_file(
            creds_conn, key_name="EMPTY_KEY", filename="e.bin", data=b"", actor="t"
        ).error_code == "ERR_INVALID_ARGS"
        assert ops.put_file(
            creds_conn, key_name="TXT_KEY", filename="t.bin", data="text", actor="t"
        ).error_code == "ERR_INVALID_ARGS"
        # 恰好等于上限 → 通过（上限是 <= 语义）
        assert ops.put_file(
            creds_conn, key_name="EQ_KEY", filename="q.bin", data=b"abcd",
            actor="t", max_bytes=4,
        ).ok
        # 主密钥缺失 → 拒绝写入且不留半条记录
        monkeypatch.delenv("SGME_CRED_KEY", raising=False)
        r = ops.put_file(creds_conn, key_name="NOKEY_FILE", filename="n.bin", data=b"abc", actor="t")
        assert (not r.ok) and r.error_code == "ERR_INTERNAL"
        assert "主密钥" in (r.message or "") and "abc" not in (r.message or "")
        assert creds_conn.execute(
            "SELECT COUNT(*) FROM credential_files WHERE key_name='NOKEY_FILE'"
        ).fetchone()[0] == 0

    def test_filename_sanitization(self, creds_conn, cred_key):
        r = ops.put_file(
            creds_conn, key_name="TRAV_KEY", filename="../../etc/passwd", data=b"x", actor="t"
        )
        assert r.ok and r.data["filename"] == "passwd"  # basename 化，穿越不生效
        r = ops.put_file(
            creds_conn, key_name="TRAV_KEY", filename="C:\\windows\\evil.bin",
            data=b"x", actor="t",
        )
        assert r.ok and r.data["filename"] == "evil.bin"
        r = ops.put_file(
            creds_conn, key_name="CTRL_KEY", filename="bad\x00na\x0bme.bin", data=b"x", actor="t"
        )
        assert r.ok and r.data["filename"] == "badname.bin"  # 控制字符被剥掉
        for bad in ("", "/", "..", "x" * 121):
            got = ops.put_file(
                creds_conn, key_name="BAD_NAME_KEY", filename=bad, data=b"x", actor="t"
            )
            assert got.error_code == "ERR_INVALID_ARGS", bad
        # key_name 仍走统一校验
        assert ops.put_file(
            creds_conn, key_name="lowercase", filename="a.bin", data=b"x", actor="t"
        ).error_code == "ERR_INVALID_ARGS"
        # sanitize_filename 单元面
        assert ops.sanitize_filename("a/b/c.bin") == "c.bin"
        assert ops.sanitize_filename(None, fallback="f.bin") == "f.bin"

    def test_conflicts_and_replacement(self, creds_conn, cred_key):
        # 已存在非 file 类型（含 cred_type 未声明）→ 409，不静默改型
        ops.write_credential(creds_conn, key_name="TEXT_KEY", value=FAKE_VALUE, actor="t")
        r = ops.put_file(creds_conn, key_name="TEXT_KEY", filename="a.bin", data=b"x", actor="t")
        assert r.error_code == "ERR_CONFLICT" and "非 file" in (r.message or "")
        # 已删键 → 409（不自动复活）
        assert ops.put_file(
            creds_conn, key_name="DEL_FILE", filename="a.bin", data=b"1", actor="t"
        ).ok
        ops.delete_credential(creds_conn, key_name="DEL_FILE", actor="t")
        assert ops.put_file(
            creds_conn, key_name="DEL_FILE", filename="a.bin", data=b"2", actor="t"
        ).error_code == "ERR_CONFLICT"
        assert creds_conn.execute(
            "SELECT COUNT(*) FROM credential_files WHERE key_name='DEL_FILE'"
        ).fetchone()[0] == 1  # 冲突不落库
        # restore 后可替换（替换而非新增）
        assert ops.restore_credential(creds_conn, key_name="DEL_FILE", actor="t").ok
        r = ops.put_file(
            creds_conn, key_name="DEL_FILE", filename="b.bin", data=b"22", actor="t"
        )
        assert r.ok and r.data["filename"] == "b.bin"
        assert creds_conn.execute(
            "SELECT COUNT(*) FROM credential_files WHERE key_name='DEL_FILE'"
        ).fetchone()[0] == 1
        row = dao.get_file(creds_conn, "DEL_FILE")
        assert cred.decrypt_bytes(row["data_cipher"]) == b"22"
        assert row["size"] == 2 and row["created_at"] <= row["updated_at"]

    def test_value_channel_rejected_for_file(self, creds_conn, cred_key):
        assert ops.put_file(
            creds_conn, key_name="F_VAL", filename="a.bin", data=b"x", actor="t"
        ).ok
        # 值端点读 → 400 指路下载端点
        r = ops.get_credential(creds_conn, "F_VAL", actor="t")
        assert r.error_code == "ERR_INVALID_ARGS" and "/file" in (r.message or "")
        # 值端点写 file 类型 → 400 指路上传端点
        r = ops.write_credential(
            creds_conn, key_name="NEW_FILE_TYPE", value=FAKE_VALUE, cred_type="file", actor="t"
        )
        assert r.error_code == "ERR_INVALID_ARGS" and "/file" in (r.message or "")
        # file 行 + value → 409
        r = ops.write_credential(creds_conn, key_name="F_VAL", value=FAKE_VALUE, actor="t")
        assert r.error_code == "ERR_CONFLICT"
        # 元数据-only 更新仍可用（v1.1 合并语义）
        r = ops.write_credential(creds_conn, key_name="F_VAL", description="新备注", actor="t")
        assert r.ok and dao.get_credential(creds_conn, "F_VAL")["description"] == "新备注"
        # file 行不可改型（清空同样拒绝）
        assert ops.write_credential(
            creds_conn, key_name="F_VAL", cred_type="", actor="t"
        ).error_code == "ERR_INVALID_ARGS"

    def test_list_export_and_read_guards(self, creds_conn, cred_key):
        ops.write_credential(creds_conn, key_name="TEXT_ONLY", value=FAKE_VALUE, actor="t")
        # 非 file 键走文件端点 → 400
        r = ops.read_file(creds_conn, "TEXT_ONLY", actor="t")
        assert r.error_code == "ERR_INVALID_ARGS" and "非文件类型" in (r.message or "")
        assert ops.put_file(
            creds_conn, key_name="EXPORT_FILE", filename="e.bin",
            data=FAKE_FILE_BYTES, actor="t",
        ).ok
        listed = {c["key_name"]: c for c in ops.list_credentials(creds_conn).data["credentials"]}
        assert listed["EXPORT_FILE"]["filename"] == "e.bin"
        assert listed["EXPORT_FILE"]["file_size"] == len(FAKE_FILE_BYTES)
        assert listed["EXPORT_FILE"]["file_sha256"] == FILE_SHA
        assert "filename" not in listed["TEXT_ONLY"]  # 文本行不带文件字段
        exp = ops.export_credentials(creds_conn, actor="t")
        assert exp.ok and exp.data["credentials"] == {"TEXT_ONLY": FAKE_VALUE}
        assert exp.data["skipped"] == [{"key_name": "EXPORT_FILE", "reason": "file"}]
        assert ops.export_credentials(
            creds_conn, only="EXPORT_FILE", actor="t"
        ).error_code == "ERR_INVALID_ARGS"
        assert ops.read_file(creds_conn, "NOPE_FILE", actor="t").error_code == "ERR_NOT_FOUND"

    def test_scope_and_expiry(self, creds_conn, cred_key, monkeypatch):
        assert ops.put_file(
            creds_conn, key_name="ADMIN_FILE", filename="a.bin", data=b"x",
            scope="admin", actor="t",
        ).ok
        r = ops.read_file(
            creds_conn, "ADMIN_FILE", actor="dsh(agent,mcp)", agent_id="dsh", is_admin=False
        )
        assert r.error_code == "ERR_FORBIDDEN"
        assert ops.read_file(creds_conn, "ADMIN_FILE", actor="t", is_admin=True).ok
        # 过期（monkeypatch ops._now）
        base = datetime(2030, 1, 1, tzinfo=UTC)
        monkeypatch.setattr(ops, "_now", lambda: base)
        assert ops.put_file(
            creds_conn, key_name="EXP_FILE", filename="e.bin", data=b"y",
            expires_at="2030-01-02T00:00:00Z", actor="t",
        ).ok
        assert ops.read_file(creds_conn, "EXP_FILE", actor="t", is_admin=True).ok
        monkeypatch.setattr(ops, "_now", lambda: base + timedelta(days=2))
        r = ops.read_file(creds_conn, "EXP_FILE", actor="t", is_admin=True)
        assert r.error_code == "ERR_FORBIDDEN" and "过期" in (r.message or "")
        denied = creds_conn.execute(
            "SELECT COUNT(*) FROM credential_audit WHERE action='denied'"
        ).fetchone()[0]
        assert denied == 2  # scope 拒绝 + 过期拒绝 各一条


class TestHttpFile:
    @pytest.fixture
    def client_on(self, cfg_on, conns, creds_conn, cred_key, tmp_path):
        return make_client(cfg_on, conns, tmp_path, credentials_conn=creds_conn)

    H_ADMIN = {"X-API-Key": ADMIN_KEY}
    H_AGENT = {"X-API-Key": AGENT_KEY}

    def test_upload_download_roundtrip(self, client_on):
        r = client_on.post(
            "/v1/credentials/HTTP_FILE/file",
            params={"filename": "备份.bin"},
            content=FAKE_FILE_BYTES,
            headers=self.H_ADMIN,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["key_name"] == "HTTP_FILE" and body["filename"] == "备份.bin"
        assert body["size"] == len(FAKE_FILE_BYTES) and body["sha256"] == FILE_SHA
        # agent 下载：字节完全一致 + 元数据响应头
        g = client_on.get("/v1/credentials/HTTP_FILE/file", headers=self.H_AGENT)
        assert g.status_code == 200 and g.content == FAKE_FILE_BYTES
        assert g.headers["content-type"] == "application/octet-stream"
        assert g.headers["x-cred-size"] == str(len(FAKE_FILE_BYTES))
        assert g.headers["x-cred-sha256"] == FILE_SHA
        assert g.headers["content-disposition"] == (
            "attachment; filename*=UTF-8''" + urllib.parse.quote("备份.bin")
        )
        # 列表带文件元数据（MCP 文本面同源）
        item = [
            c for c in client_on.get("/v1/credentials", headers=self.H_AGENT).json()["credentials"]
            if c["key_name"] == "HTTP_FILE"
        ][0]
        assert item["filename"] == "备份.bin"
        assert item["file_size"] == len(FAKE_FILE_BYTES) and item["file_sha256"] == FILE_SHA
        # 值端点 → 400 指路下载端点
        v = client_on.get("/v1/credentials/HTTP_FILE", headers=self.H_AGENT)
        assert v.status_code == 400 and "/file" in v.json()["error"]["message"]
        # agent 上传越权；缺 filename → 422
        assert client_on.post(
            "/v1/credentials/HTTP_FILE/file", params={"filename": "x.bin"},
            content=b"x", headers=self.H_AGENT,
        ).status_code == 403
        assert client_on.post(
            "/v1/credentials/HTTP_FILE2/file", content=b"x", headers=self.H_ADMIN
        ).status_code == 422

    def test_413_with_small_max_file_bytes(self, cfg_on, conns, creds_conn, cred_key, tmp_path):
        cfg = dict(cfg_on)
        cfg["credentials"] = {"enabled": True, "max_file_bytes": 16}
        client = make_client(cfg, conns, tmp_path, credentials_conn=creds_conn)
        r = client.post(
            "/v1/credentials/TOO_BIG/file", params={"filename": "a.bin"},
            content=b"x" * 17, headers=self.H_ADMIN,
        )
        assert r.status_code == 413 and r.json()["error"]["code"] == "ERR_TOO_LARGE"
        ok = client.post(
            "/v1/credentials/JUST_FIT/file", params={"filename": "a.bin"},
            content=b"x" * 16, headers=self.H_ADMIN,
        )
        assert ok.status_code == 200 and ok.json()["size"] == 16
        assert client.get("/v1/credentials/TOO_BIG/file", headers=self.H_AGENT).status_code == 404

    def test_scope_expiry_and_soft_delete(self, client_on, monkeypatch):
        assert client_on.post(
            "/v1/credentials/SCOPED_FILE/file",
            params={"filename": "s.bin", "scope": "admin"},
            content=b"secret-bytes", headers=self.H_ADMIN,
        ).status_code == 200
        assert client_on.get("/v1/credentials/SCOPED_FILE/file", headers=self.H_AGENT).status_code == 403
        assert client_on.get("/v1/credentials/SCOPED_FILE/file", headers=self.H_ADMIN).status_code == 200
        # 过期 → 403（monkeypatch ops._now 推进时间）
        exp = _iso(datetime.now(UTC) + timedelta(hours=1))
        assert client_on.post(
            "/v1/credentials/EXP_FILE/file",
            params={"filename": "e.bin", "expires_at": exp},
            content=b"soon-stale", headers=self.H_ADMIN,
        ).status_code == 200
        monkeypatch.setattr(ops, "_now", lambda: datetime.now(UTC) + timedelta(hours=2))
        assert client_on.get("/v1/credentials/EXP_FILE/file", headers=self.H_AGENT).status_code == 403

    def test_soft_delete_restore_and_export(self, client_on):
        assert client_on.post(
            "/v1/credentials/SOFT_FILE/file", params={"filename": "soft.bin"},
            content=b"soft-bytes", headers=self.H_ADMIN,
        ).status_code == 200
        assert client_on.get("/v1/credentials/SOFT_FILE/file", headers=self.H_AGENT).status_code == 200
        assert client_on.delete("/v1/credentials/SOFT_FILE", headers=self.H_ADMIN).status_code == 200
        assert client_on.get("/v1/credentials/SOFT_FILE/file", headers=self.H_AGENT).status_code == 404
        assert client_on.post(
            "/v1/credentials/SOFT_FILE/restore", headers=self.H_ADMIN
        ).status_code == 200
        g = client_on.get("/v1/credentials/SOFT_FILE/file", headers=self.H_AGENT)
        assert g.status_code == 200 and g.content == b"soft-bytes"
        # export：全量 skipped reason=file；only 含 file 键 → 400
        e = client_on.get("/v1/credentials/export", headers=self.H_AGENT)
        assert e.status_code == 200
        assert e.json()["skipped"] == [{"key_name": "SOFT_FILE", "reason": "file"}]
        f = client_on.get("/v1/credentials/export?only=SOFT_FILE", headers=self.H_AGENT)
        assert f.status_code == 400 and "/file" in f.json()["error"]["message"]

