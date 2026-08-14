"""tests/test_operations_config.py：operations 层 config 模块测试（v0.7 Batch-1）。

⚠️ 命名雷区：被测模块是 ``sgme.operations.config``，与 ``sgme.config``
是两个不同模块。本文件用 ``sgme_config`` 指代后者，前者一律走完整路径导入。

覆盖：
1. get_config（宽松）/ get_config_section（严格）/ update_config（HTTP 版）/
   update_config_section（MCP 版）均返回 OperationResult
2. 信息超集字段完整（section / section_config / config / writable_sections）
3. **键序守护**：全量 config 的键序取自 CONFIG_SECTIONS 原生迭代序，
   与 v0.6 逐字节一致（若有人"顺手 sorted()"，本测试会红）
4. **失败路径**（本切片重点补齐的错误翻译分支）：
   - HTTP 单段读未知段 → ERR_NOT_FOUND，文案**带**可用段列表
   - MCP 读未知段 → **不报错**（宽松语义，config: null）
   - HTTP 多段写未知段 → ERR_INVALID_ARGS（400），文案**不带**列表
   - HTTP 单段写未知段 → ERR_NOT_FOUND（404），文案**带**列表
   - MCP 写未知段 → ERR_NOT_FOUND，文案**不带**列表
   - 落盘失败：HTTP **上抛**（500 内部错误）/ MCP **捕获**（error JSON）
5. **契约等价性**（最关键）：GET/PUT/POST /v1/admin/config 与 MCP
   config_get/config_update 经 operations 包装后，字段集合与顺序仍与 v0.6 一致
6. 白名单过滤：单段写过滤未知键；多段写**不过滤**（v0.6 既有不一致，原样保留）

零真实 DB / 零真实配置文件：conftest 的 autouse fixture 已把
SGME_CONFIG_PATH 指向 tmp_path，落盘只写临时文件。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations.config import (
    get_config,
    get_config_section,
    get_http_payload,
    get_mcp_payload,
    update_config,
    update_config_section,
    update_payload,
)
from sgme.operations.errors import ERR_INTERNAL, ERR_INVALID_ARGS, ERR_NOT_FOUND, OperationResult
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# ---------- v0.6 冻结契约（改造前逐字段抄录，任何变动即破坏性变更） ----------

HTTP_GET_ALL_KEYS = ["config", "writable_sections"]
HTTP_GET_SECTION_KEYS = ["section", "config"]
MCP_GET_ALL_KEYS = ["config"]                       # MCP 无 writable_sections
MCP_GET_SECTION_KEYS = ["section", "config"]
UPDATE_MULTI_KEYS = ["status", "config"]
UPDATE_SINGLE_KEYS = ["status", "section", "config"]

# 可写段白名单：动态派生自 sgme.config.CONFIG_SECTIONS（v0.7 阶段 4 扩展后含 wiki/skills_hub/logging）
from sgme import config as _sgme_config_mod

WRITABLE_SECTIONS = sorted(_sgme_config_mod.CONFIG_SECTIONS)

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    """独立配置副本（每个测试一份，避免就地修改互相污染）。"""
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    # 防御纵深：conftest 已设，这里再确认一次落盘只写 tmp_path
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    m, s, w = conns
    return create_app(
        cfg=cfg, mem_conn=m, session_conn=s, wiki_conn=w,
        admin_key="test-admin-key", agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def lenient_client(app):
    """不把服务端异常再抛给测试的 client（验证 500 兜底形态专用）。"""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mcp(conns, cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    m, s, w = conns
    bind_app_state({"cfg": cfg, "mem_conn": m, "session_conn": s, "wiki_conn": w})
    return build_mcp_server()


@pytest.fixture
def persist_fails(monkeypatch):
    """让 persist_config 抛 OSError（模拟磁盘满 / 目录写保护）。

    比 chmod 目录更可靠：Windows 上 chmod 对目录基本无效，
    monkeypatch 才能跨平台稳定复现落盘失败分支。
    """
    def _boom(cfg, config_path=None):
        raise OSError("磁盘写保护")

    monkeypatch.setattr(sgme_config, "persist_config", _boom)


# ---------- 工具 ----------

def _call_mcp(mcp_server, name: str, args: dict) -> str:
    raw = asyncio.run(mcp_server.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    return "\n".join(c.text for c in results if getattr(c, "text", None))


# ---------- 1. 返回类型与超集结构 ----------

def test_get_config_returns_operation_result_ok(cfg):
    """get_config 返回 OperationResult(ok=True)，本操作不失败。"""
    # Act
    res = get_config(cfg)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None


def test_data_is_protocol_agnostic_superset(cfg):
    """data 同时携带 section / section_config / config / writable_sections。"""
    # Act
    data = get_config(cfg, section="refine").data

    # Assert
    assert set(data.keys()) == {"section", "section_config", "config", "writable_sections"}
    assert data["section"] == "refine"
    assert data["section_config"] == cfg["refine"]
    assert data["writable_sections"] == WRITABLE_SECTIONS
    assert set(data["config"].keys()) == set(WRITABLE_SECTIONS)


@pytest.mark.parametrize("falsy", [None, ""])
def test_get_config_falsy_section_is_whole_read(cfg, falsy):
    """section 为 None / 空串 → 整体读（照抄 v0.6 MCP 的 ``if section:`` 真值口径）。"""
    # Act
    data = get_config(cfg, section=falsy).data

    # Assert
    assert data["section"] is None
    assert data["section_config"] is None


# ---------- 2. 键序守护（逐字节等价的关键） ----------

def test_all_sections_key_order_matches_native_iteration(cfg):
    """全量 config 键序 == CONFIG_SECTIONS 原生迭代序（**不是** sorted）。

    v0.6 两端都写 ``{s: cfg.get(s) for s in CONFIG_SECTIONS}``；
    若有人把它改成 sorted()，JSON 键序变化即破坏契约，本测试守这条线。
    """
    # Act
    data = get_config(cfg).data

    # Assert
    assert list(data["config"].keys()) == list(sgme_config.CONFIG_SECTIONS)
    # writable_sections 则**是**排序的（v0.6 如此，两者不同源，勿"统一"）
    assert data["writable_sections"] == sorted(sgme_config.CONFIG_SECTIONS)


# ---------- 3. 读路径失败分支：严格 vs 宽松 ----------

def test_get_config_section_unknown_is_not_found_with_list(cfg):
    """HTTP 单段读未知段 → ERR_NOT_FOUND，文案**带**可用段列表。"""
    # Act
    res = get_config_section(cfg, section="不存在的段")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == f"未知配置段: 不存在的段（可用: {WRITABLE_SECTIONS}）"
    assert not res.details  # 404 体必须只有 code/message 两键


def test_get_config_unknown_section_is_lenient(cfg):
    """MCP 读未知段 → **不报错**（宽松语义），config 为 null——v0.6 既有行为。"""
    # Act
    res = get_config(cfg, section="不存在的段")

    # Assert
    assert res.ok is True
    assert res.data["section"] == "不存在的段"
    assert res.data["section_config"] is None


def test_strict_and_lenient_share_success_shape(cfg):
    """严格版校验通过后完全委托宽松版，两者成功态同源。"""
    # Act
    strict = get_config_section(cfg, section="refine").data
    lenient = get_config(cfg, section="refine").data

    # Assert
    assert strict == lenient


# ---------- 4. 读路径投影差异 ----------

def test_get_projections_differ_only_on_writable_sections(cfg):
    """整体读：HTTP 多一个 writable_sections；单段读两端同形——历史差异。"""
    # Arrange
    whole = get_config(cfg).data
    single = get_config(cfg, section="search").data

    # Act / Assert：整体读
    assert list(get_http_payload(whole).keys()) == HTTP_GET_ALL_KEYS
    assert list(get_mcp_payload(whole).keys()) == MCP_GET_ALL_KEYS
    assert get_http_payload(whole)["config"] is get_mcp_payload(whole)["config"]

    # Act / Assert：单段读两端完全一致
    assert list(get_http_payload(single).keys()) == HTTP_GET_SECTION_KEYS
    assert list(get_mcp_payload(single).keys()) == MCP_GET_SECTION_KEYS
    assert get_http_payload(single) == get_mcp_payload(single)


# ---------- 5. 写路径：成功态 ----------

def test_update_config_single_section(cfg):
    """HTTP 单段写：白名单过滤 + 深合并 + 落盘，返回 status/section/config。"""
    # Act
    res = update_config(cfg, section="refine", values={"refine_on_append": True})

    # Assert
    assert res.ok is True
    assert res.data["status"] == "ok"
    assert res.data["section"] == "refine"
    assert cfg["refine"]["refine_on_append"] is True
    assert list(update_payload(res.data).keys()) == UPDATE_SINGLE_KEYS


def test_update_config_single_filters_unknown_keys(cfg):
    """单段写过滤白名单外的键（防未知键注入）。"""
    # Act
    update_config(cfg, section="refine",
                  values={"refine_on_append": True, "恶意键": "x"})

    # Assert
    assert cfg["refine"]["refine_on_append"] is True
    assert "恶意键" not in cfg["refine"]


def test_update_config_multi_section(cfg):
    """HTTP 多段写：values 的键即段名，返回 status/config（无 section）。"""
    # Act
    res = update_config(cfg, section=None, values={"refine": {"refine_on_append": True}})

    # Assert
    assert res.ok is True
    assert res.data["section"] is None
    assert cfg["refine"]["refine_on_append"] is True
    assert list(update_payload(res.data).keys()) == UPDATE_MULTI_KEYS


def test_update_config_multi_does_not_filter_keys(cfg):
    """⚠️ 多段写**不过滤**白名单（v0.6 既有不一致，抽取时不得"顺手对齐"）。"""
    # Act
    update_config(cfg, section=None, values={"refine": {"未在白名单的键": 42}})

    # Assert
    assert cfg["refine"]["未在白名单的键"] == 42


@pytest.mark.parametrize("empty", [None, {}])
def test_update_config_empty_values_is_noop_but_persists(cfg, empty):
    """values 为 None / 空 dict → 归一为空 dict，仍走落盘（v0.6 行为）。"""
    # Act
    res = update_config(cfg, section="refine", values=empty)

    # Assert
    assert res.ok is True
    assert res.data["status"] == "ok"


def test_update_config_section_mcp_version(cfg):
    """MCP 单段写：与 HTTP 单段写成功态同形。"""
    # Act
    res = update_config_section(cfg, section="l1", values={"chunk_size": 999})

    # Assert
    assert res.ok is True
    assert cfg["l1"]["chunk_size"] == 999
    assert list(update_payload(res.data).keys()) == UPDATE_SINGLE_KEYS


# ---------- 6. 写路径失败分支：三处两版文案 ----------

def test_update_multi_unknown_section_is_invalid_args_short_message(cfg):
    """HTTP 多段写未知段 → ERR_INVALID_ARGS（400），文案**不带**可用段列表。"""
    # Act
    res = update_config(cfg, section=None, values={"不存在的段": {"k": 1}})

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS
    assert res.message == "未知配置段: 不存在的段"


def test_update_single_unknown_section_is_not_found_verbose_message(cfg):
    """HTTP 单段写未知段 → ERR_NOT_FOUND（404），文案**带**可用段列表。"""
    # Act
    res = update_config(cfg, section="不存在的段", values={"k": 1})

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == f"未知配置段: 不存在的段（可用: {WRITABLE_SECTIONS}）"


def test_update_config_section_unknown_short_message(cfg):
    """MCP 写未知段 → ERR_NOT_FOUND，文案**不带**可用段列表（与 HTTP 单段写不同）。"""
    # Act
    res = update_config_section(cfg, section="不存在的段", values={"k": 1})

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == "未知配置段: 不存在的段"


def test_update_multi_applies_before_failing(cfg):
    """⚠️ 多段写「边改边校验」：报错前已 apply 的段留在内存 cfg（但未落盘）。

    v0.6 既有行为，抽取时原样保留。dict 保序，refine 在未知段之前。
    """
    # Act
    res = update_config(cfg, section=None, values={
        "refine": {"refine_on_append": True},
        "不存在的段": {"k": 1},
    })

    # Assert
    assert res.ok is False
    assert cfg["refine"]["refine_on_append"] is True  # 已改（内存）


# ---------- 7. 写路径失败分支：落盘失败（两端处理不同） ----------

def test_http_update_persist_failure_bubbles_up(cfg, persist_fails):
    """HTTP 版落盘失败**不捕获**——异常上抛，还原 v0.6 的 500 内部错误形态。"""
    # Act / Assert
    with pytest.raises(OSError, match="磁盘写保护"):
        update_config(cfg, section="refine", values={"refine_on_append": True})


def test_http_update_multi_persist_failure_bubbles_up(cfg, persist_fails):
    """多段形态同样不捕获落盘异常。"""
    # Act / Assert
    with pytest.raises(OSError, match="磁盘写保护"):
        update_config(cfg, section=None, values={"refine": {"refine_on_append": True}})


def test_mcp_update_persist_failure_becomes_error_result(cfg, persist_fails):
    """MCP 版落盘失败**就地捕获** → ERR_INTERNAL「配置落盘失败: {e}」（v0.6 行为）。"""
    # Act
    res = update_config_section(cfg, section="refine", values={"refine_on_append": True})

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert res.message == "配置落盘失败: 磁盘写保护"


# ---------- 8. 契约等价性（最关键）：HTTP ----------

def test_http_get_all_contract_unchanged(client):
    """GET /v1/admin/config 字段集合与顺序仍与 v0.6 一致。"""
    # Act
    resp = client.get("/v1/admin/config", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_GET_ALL_KEYS
    assert body["writable_sections"] == WRITABLE_SECTIONS
    assert set(body["config"].keys()) == set(WRITABLE_SECTIONS)


def test_http_get_section_contract_unchanged(client, cfg):
    """GET /v1/admin/config/{section} 字段集合与顺序仍与 v0.6 一致。"""
    # Act
    resp = client.get("/v1/admin/config/refine", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_GET_SECTION_KEYS
    assert body["section"] == "refine"
    assert body["config"] == cfg["refine"]


def test_http_get_unknown_section_404_contract_unchanged(client):
    """GET /v1/admin/config/{未知段} → 404，文案带可用段列表，error 只有两键。"""
    # Act
    resp = client.get("/v1/admin/config/nosuch", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 404, resp.text
    err = resp.json()["error"]
    assert list(err.keys()) == ["code", "message"]
    assert err["code"] == "ERR_NOT_FOUND"
    assert err["message"] == f"未知配置段: nosuch（可用: {WRITABLE_SECTIONS}）"


@pytest.mark.parametrize("method", ["put", "post"])
def test_http_update_single_contract_unchanged(client, method):
    """PUT|POST /v1/admin/config 单段形态响应键集合与顺序仍与 v0.6 一致。"""
    # Act
    resp = getattr(client, method)(
        "/v1/admin/config", headers=ADMIN_HEADERS,
        json={"section": "refine", "values": {"refine_on_append": True}},
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == UPDATE_SINGLE_KEYS
    assert body["status"] == "ok"
    assert body["section"] == "refine"
    assert body["config"]["refine_on_append"] is True


@pytest.mark.parametrize("method", ["put", "post"])
def test_http_update_multi_contract_unchanged(client, method):
    """PUT|POST /v1/admin/config 多段形态响应键集合与顺序仍与 v0.6 一致。"""
    # Act
    resp = getattr(client, method)(
        "/v1/admin/config", headers=ADMIN_HEADERS,
        json={"section": None, "values": {"refine": {"refine_on_append": False}}},
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == UPDATE_MULTI_KEYS
    assert body["status"] == "ok"
    assert set(body["config"].keys()) == set(WRITABLE_SECTIONS)
    assert body["config"]["refine"]["refine_on_append"] is False


def test_http_update_single_unknown_section_404(client):
    """PUT 单段写未知段 → 404 + 带列表文案（v0.6 状态码与文案）。"""
    # Act
    resp = client.put("/v1/admin/config", headers=ADMIN_HEADERS,
                      json={"section": "nosuch", "values": {"k": 1}})

    # Assert
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == f"未知配置段: nosuch（可用: {WRITABLE_SECTIONS}）"


def test_http_update_multi_unknown_section_400(client):
    """PUT 多段写未知段 → 400 + 不带列表文案（v0.6 状态码与文案）。"""
    # Act
    resp = client.put("/v1/admin/config", headers=ADMIN_HEADERS,
                      json={"section": None, "values": {"nosuch": {"k": 1}}})

    # Assert
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "ERR_INVALID_ARGS"
    assert err["message"] == "未知配置段: nosuch"


def test_http_update_persist_failure_returns_500(lenient_client, persist_fails):
    """HTTP 落盘失败 → 500 「内部错误: ...」（全局处理器兜底，非 operations 失败态）。"""
    # Act
    resp = lenient_client.put("/v1/admin/config", headers=ADMIN_HEADERS,
                              json={"section": "refine", "values": {"refine_on_append": True}})

    # Assert
    assert resp.status_code == 500, resp.text
    err = resp.json()["error"]
    assert err["code"] == "ERR_INTERNAL"
    assert err["message"] == "内部错误: 磁盘写保护（可查看服务日志 sgme.log 获取堆栈）"


def test_http_config_persists_to_tmp_file_only(client, tmp_path):
    """落盘只写 tmp_path（SGME_CONFIG_PATH 隔离生效），绝不污染仓库 config/sgme.yaml。"""
    # Act
    resp = client.put("/v1/admin/config", headers=ADMIN_HEADERS,
                      json={"section": "l1", "values": {"chunk_size": 4242}})

    # Assert
    assert resp.status_code == 200, resp.text
    written = tmp_path / "sgme_test.yaml"
    assert written.exists()
    assert "4242" in written.read_text(encoding="utf-8")


# ---------- 9. 契约等价性（最关键）：MCP ----------

def test_mcp_config_get_all_contract_unchanged(mcp):
    """MCP config_get 整体读 → 只有 config 一键（**无** writable_sections）。"""
    # Act
    body = json.loads(_call_mcp(mcp, "config_get", {}))

    # Assert
    assert list(body.keys()) == MCP_GET_ALL_KEYS
    assert set(body["config"].keys()) == set(WRITABLE_SECTIONS)


def test_mcp_config_get_section_contract_unchanged(mcp, cfg):
    """MCP config_get 单段读 → {"section", "config"}，与 HTTP 同形。"""
    # Act
    body = json.loads(_call_mcp(mcp, "config_get", {"section": "search"}))

    # Assert
    assert list(body.keys()) == MCP_GET_SECTION_KEYS
    assert body["section"] == "search"
    assert body["config"] == cfg["search"]


def test_mcp_config_get_unknown_section_is_lenient(mcp):
    """MCP config_get 未知段 → **不报错**，config 为 null（与 HTTP 的 404 是历史差异）。"""
    # Act
    body = json.loads(_call_mcp(mcp, "config_get", {"section": "nosuch"}))

    # Assert
    assert body == {"section": "nosuch", "config": None}
    assert "error" not in body


def test_mcp_config_update_contract_unchanged(mcp, cfg):
    """MCP config_update 成功体键集合与顺序仍与 v0.6 一致。"""
    # Act
    body = json.loads(_call_mcp(mcp, "config_update",
                                {"section": "l1", "values": {"chunk_size": 1234}}))

    # Assert
    assert list(body.keys()) == UPDATE_SINGLE_KEYS
    assert body["status"] == "ok"
    assert body["section"] == "l1"
    assert body["config"]["chunk_size"] == 1234
    assert cfg["l1"]["chunk_size"] == 1234  # 热生效（就地改同一 cfg 对象）


def test_mcp_config_update_unknown_section_contract_unchanged(mcp):
    """MCP config_update 未知段 → {"error": "未知配置段: nosuch"}（不带可用段列表）。"""
    # Act
    body = json.loads(_call_mcp(mcp, "config_update", {"section": "nosuch", "values": {}}))

    # Assert
    assert body == {"error": "未知配置段: nosuch"}


def test_mcp_config_update_persist_failure_contract_unchanged(mcp, persist_fails):
    """MCP config_update 落盘失败 → {"error": "配置落盘失败: ..."}（不上抛，v0.6 行为）。"""
    # Act
    body = json.loads(_call_mcp(mcp, "config_update",
                                {"section": "refine", "values": {"refine_on_append": True}}))

    # Assert
    assert body == {"error": "配置落盘失败: 磁盘写保护"}


def test_http_and_mcp_agree_on_shared_fields(client, mcp):
    """同一 cfg 下两端共有字段取值一致（差异只在 writable_sections 与未知段语义）。"""
    # Act
    http_body = client.get("/v1/admin/config", headers=ADMIN_HEADERS).json()
    mcp_body = json.loads(_call_mcp(mcp, "config_get", {}))

    # Assert
    assert http_body["config"] == mcp_body["config"]
    assert list(http_body["config"].keys()) == list(mcp_body["config"].keys())
