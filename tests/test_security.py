"""tests/test_security.py：入口安全加固测试基座（ST-55，方案 v3）。

本文件为 ST-55 安全加固批次（T-255 ~ T-266）的共享测试面，约定（S5，方案 §四）：

1. SecurityState 实例随 create_app 创建（T-262 起挂 app.state.security_state）、
   可注入可重置；HTTP/MCP 两 app 共享同一实例——测试不得各自 new 单例互踩。
2. TestClient 的 client.host 为 "testclient"，与 net_guard.LOCALHOST_HOSTS 归同一
   「本地」组（is_loopback_source 返回 True）——集成用例默认视为回环来源，不会因
   失败累计触发封禁。
3. 需要跨阈值行为的用例（封禁/升级阶梯）必须【显式设置配置】
   （如 auth_fail_threshold=20）；缺省 threshold=0 = 观察期（只计数不封）。
4. 显式阈值用例结束前后注意重置共享状态（实例注入重建或调用 reset 接口），
   防同进程内用例间串扰。

涉及卡片：
- T-255：请求体上限（I3 前置 3 项）
- T-256：resolve_client_ip / ip_key（U1，追加测试类）
- T-258：security_state 状态机（U2 单元部分，追加）
- T-260：SecurityGate（U2 / U3 / U9，追加）
- T-261：常数时间比较（U8，追加）
- T-262 / T-263：接线集成（I1 / I2 / I3 收口，追加）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.server.app import create_app

# ---------- 夹具（S5 基座约定） ----------


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path，不触真实 data/）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


# ---------- I3：请求体上限（T-255，§2.8-1 / 细节 9-10 校准） ----------


class TestRequestBodyLimit:
    """请求体上限：框架原生 RequestBodyLimitMiddleware。

    断言口径（校准结论）：
    - 413 形态 = text/plain "Content Too Large"（框架默认，非 JSON detail）；
    - chunked（无 content-length）超限同样 413，挂点 = 读取请求体的业务端点
      （/v1/append）；不读 body 的路径不触发 413 属预期。
    """

    def _make_client(self, conns, cfg, tmp_path, max_body_bytes: int) -> TestClient:
        mem_conn, session_conn, wiki_conn = conns
        cfg["server"]["max_body_bytes"] = max_body_bytes
        app = create_app(
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            admin_key="test-admin-key-security",
            agent_key="test-agent-key-security",
            bearer_token="",
            agent_store_path=tmp_path / "agent_keys.json",
        )
        return TestClient(app)

    def test_oversize_returns_413(self, conns, cfg, tmp_path):
        """非 chunked（带 content-length）超限 → 413。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=1024)
        r = client.post(
            "/v1/append",
            json={
                "session_key": "sec-t255-a",
                "started_at": "2026-10-11T00:00:00Z",
                "content": "# 2026-10-11T00:00:00Z user\n" + "x" * 4096,
            },
            headers={"X-API-Key": "test-agent-key-security"},
        )
        assert r.status_code == 413
        assert "text/plain" in r.headers.get("content-type", "")
        assert "Content Too Large" in r.text

    def test_chunked_oversize_returns_413(self, conns, cfg, tmp_path):
        """chunked（无 content-length）超限 → 413（挂 /v1/append 读 body 端点）。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=1024)
        payload = (
            '{"session_key":"sec-t255-b","started_at":"2026-10-11T00:00:00Z","content":"'
            + "x" * 4096
            + '"}'
        )
        body = payload.encode()
        r = client.post(
            "/v1/append",
            content=body,
            headers={
                "X-API-Key": "test-agent-key-security",
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        assert r.status_code == 413
        assert "text/plain" in r.headers.get("content-type", "")
        assert "Content Too Large" in r.text

    def test_small_request_passes(self, conns, cfg, tmp_path):
        """上限内正常请求 → 200（/v1/health 不需要 key，直接可用）。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=1024)
        r = client.get("/v1/health")
        assert r.status_code == 200

    def test_zero_disables_limit(self, conns, cfg, tmp_path):
        """max_body_bytes=0 → 不装配体限中间件（显式关闭语义）。"""
        client = self._make_client(conns, cfg, tmp_path, max_body_bytes=0)
        body = b"x" * 4096
        r = client.post(
            "/v1/append",
            content=body,
            headers={
                "X-API-Key": "test-agent-key-security",
                "Content-Type": "application/json",
            },
        )
        # 体限关闭：不因大小 413（业务校验失败形态为 4xx JSON，非 text/plain 413）
        assert not (
            r.status_code == 413 and "text/plain" in r.headers.get("content-type", "")
        )


# ---------- I3 收口位（T-262 落） ----------
# 「413 不产生 401/403 失败计数副作用」为跨卡断言：
# 本卡落 413 形态部分；完整副作用断言在 T-262（接线后）追加到此类。
