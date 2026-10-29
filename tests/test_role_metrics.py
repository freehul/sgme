"""tests/test_role_metrics.py：D-74 真实感指标埋点（T-308）。

覆盖验收：
① 四项确定性指标纯函数边界（首句长度 / 感叹号率 / 复述率 / AI 味句式）
② 记录聚合口径（均长 / 首句中位 / 感叹号均 / 复述率 / AI 味率 / 盲读分均值）
③ baseline 只设一次（后续 set_as_baseline 不覆盖）
④ latest 每次覆盖且回复原文截断 300 字；runs 封顶 20 且不含原文
⑤ sgme_care / sgme_violations / sgme_metrics 三命名空间共存不丢
⑥ memory.db 三库字节不变（隔离铁律）
⑦ 角色不存在 → ERR_NOT_FOUND；参数非法 → ERR_INVALID_ARGS
⑧ 越界只读聚合（total / 今日 / by_kind）
⑨ HTTP 两端点接线
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.operations import role_metrics as metrics_op
from sgme.operations.errors import ERR_INVALID_ARGS, ERR_NOT_FOUND
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

NOW = "2026-10-30T02:00:00Z"


@pytest.fixture
def roles_dir(tmp_path, monkeypatch):
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    return rd


@pytest.fixture
def conns(tmp_path):
    from sgme.data import db as db_mod

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def card(roles_dir):
    """带 sgme_care 命名空间的测试卡（验证多命名空间共存）。"""
    roles_mod.save_role(roles_dir, "butler", {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "管家",
            "description": "测试卡",
            "extensions": {"sgme_care": {"greeting_templates": ["早。"]}},
        },
    })


def _items() -> list[dict]:
    """三条手工算过的样本（断言用真值，不让实现自证）。"""
    return [
        {"q": "今天有点累", "reply": "今天有点累，什么都不想干。", "like_human": 80},
        {"q": "嗯", "reply": "嗯，我在。"},
        {"q": "帮我订外卖", "reply": "好的，马上为您安排！", "like_human": 40},
    ]


# ---------- ① 纯函数边界 ----------

def test_first_sentence_len_edges():
    assert metrics_op.first_sentence_len("你好。") == 2
    assert metrics_op.first_sentence_len("。") == 1          # 首字符即标点：不算首句结束
    assert metrics_op.first_sentence_len("") == 0
    assert metrics_op.first_sentence_len("没有标点的句子") == 7
    assert metrics_op.first_sentence_len("第一行\n第二行。") == 3  # 换行也算句末


def test_exclaim_rate_edges():
    assert metrics_op.exclaim_rate("你好世界！") == 20.0
    assert metrics_op.exclaim_rate("无感叹号") == 0.0
    assert metrics_op.exclaim_rate("") == 0.0                 # 空文本不除零
    assert metrics_op.exclaim_rate("！!") == 100.0            # 中英文叹号都计


def test_restating_edges():
    assert metrics_op.is_restating("今天有点累", "今天有点累，什么都不想干") is True
    assert metrics_op.is_restating("帮我订外卖", "好的，马上为您安排！") is False
    assert metrics_op.is_restating("嗯", "嗯，我在。") is False  # 问题无二字片段 → 不算复述


def test_ai_taste_hits():
    hits = metrics_op.ai_taste_hits("作为一个AI助手，很高兴为您服务。")
    assert 0 in hits and 8 in hits                            # 命中两条，返回下标
    assert metrics_op.ai_taste_hits("今天想吃面。") == []


# ---------- ② 记录与聚合口径 ----------

def test_record_aggregates(roles_dir, card):
    res = metrics_op.metrics_record(
        "butler", _items(), label="t1", set_as_baseline=True, now=NOW)

    assert res.ok is True
    d = res.data
    assert d["n"] == 3
    assert d["avg_chars"] == 9.3                # (13+5+10)/3
    assert d["first_sentence_median"] == 9      # sorted[4,9,12][1]
    assert d["exclaim_rate"] == 3.33            # (0+0+10)/3
    assert d["restate_rate"] == 0.333           # 1/3
    assert d["ai_taste_rate"] == 0.0
    assert d["like_human_avg"] == 60.0          # (80+40)/2，None 不计
    assert d["baseline_set"] is True

    box = roles_mod.load_metrics(roles_mod.get_role(roles_dir, "butler"))
    assert box["baseline"]["label"] == "t1"
    assert box["baseline"]["n"] == 3
    assert "items" not in box["baseline"]       # 基线/历史只存聚合
    assert len(box["latest"]["items"]) == 3
    assert box["latest"]["items"][0]["restating"] is True
    assert box["runs"][0]["label"] == "t1"


# ---------- ③ baseline 只设一次 ----------

def test_baseline_set_once(roles_dir, card):
    metrics_op.metrics_record("butler", _items(), label="first",
                              set_as_baseline=True, now=NOW)
    res = metrics_op.metrics_record(
        "butler", [{"q": "q", "reply": "短句。"}], label="second",
        set_as_baseline=True, now="2026-10-30T03:00:00Z")

    assert res.ok is True
    box = roles_mod.load_metrics(roles_mod.get_role(roles_dir, "butler"))
    assert box["baseline"]["label"] == "first"      # 不覆盖
    assert box["latest"]["label"] == "second"       # latest 覆盖
    assert box["baseline"]["n"] == 3
    assert len(box["runs"]) == 2


def test_no_baseline_without_flag(roles_dir, card):
    res = metrics_op.metrics_record("butler", _items(), label="plain", now=NOW)
    assert res.ok is True
    assert res.data["baseline_set"] is False
    box = roles_mod.load_metrics(roles_mod.get_role(roles_dir, "butler"))
    assert "baseline" not in box


# ---------- ④ latest 截断与 runs 封顶 ----------

def test_reply_truncated_300(roles_dir, card):
    long_reply = "哈" * 400
    res = metrics_op.metrics_record(
        "butler", [{"q": "q", "reply": long_reply}], now=NOW)
    assert res.ok is True
    item = roles_mod.load_metrics(roles_mod.get_role(roles_dir, "butler"))["latest"]["items"][0]
    assert len(item["reply"]) == 300              # 原文截断
    assert item["chars"] == 400                   # 长度按原文计
    assert item["first_sentence_len"] == 400


def test_runs_cap_20(roles_dir, card):
    for i in range(21):
        res = metrics_op.metrics_record(
            "butler", [{"q": "q", "reply": f"第{i}轮。"}],
            label=f"r{i}", now=f"2026-10-30T02:{i:02d}:00Z")
        assert res.ok is True
    box = roles_mod.load_metrics(roles_mod.get_role(roles_dir, "butler"))
    assert len(box["runs"]) == 20
    assert box["runs"][0]["label"] == "r1"        # 最旧的 r0 被挤掉
    assert all("items" not in r for r in box["runs"])


# ---------- ⑤ 命名空间共存不丢 ----------

def test_namespaces_coexist(roles_dir, card):
    roles_mod.append_violation(roles_dir, "butler", "service_tone", "很高兴为您服务。", now=NOW)
    metrics_op.metrics_record("butler", _items(), label="t1", now=NOW)

    data = roles_mod.get_role(roles_dir, "butler")["data"]
    ext = data["extensions"]
    assert ext["sgme_care"]["greeting_templates"] == ["早。"]
    assert len(ext["sgme_violations"]["entries"]) == 1
    assert ext["sgme_metrics"]["latest"]["label"] == "t1"


# ---------- ⑥ memory.db 零写入（隔离铁律） ----------

def test_memory_db_untouched(tmp_path, roles_dir, card):
    from sgme.data import db as db_mod

    mem_conn, _, _ = db_mod.init_databases(tmp_path / "data")

    def db_bytes() -> dict:
        return {f.name: f.read_bytes() for f in sorted((tmp_path / "data").glob("*.db"))}

    before = db_bytes()
    res = metrics_op.metrics_record("butler", _items(), label="t1", now=NOW)
    metrics_op.metrics_get("butler", now=NOW)
    after = db_bytes()

    assert res.ok is True
    assert before == after
    db_mod.close(mem_conn)


# ---------- ⑦ 错误路径 ----------

def test_role_not_found(roles_dir):
    assert metrics_op.metrics_record("ghost", _items()).error_code == ERR_NOT_FOUND
    assert metrics_op.metrics_get("ghost").error_code == ERR_NOT_FOUND


def test_invalid_args(roles_dir, card):
    assert metrics_op.metrics_record("butler", []).error_code == ERR_INVALID_ARGS
    assert metrics_op.metrics_record(
        "butler", [{"q": "q", "reply": "  "}]).error_code == ERR_INVALID_ARGS
    assert metrics_op.metrics_record(
        "butler", [{"q": "q", "reply": "好。", "like_human": 101}]).error_code == ERR_INVALID_ARGS
    assert metrics_op.metrics_record(
        "butler", [{"q": "q", "reply": "好。", "like_human": "x"}]).error_code == ERR_INVALID_ARGS
    assert metrics_op.metrics_record(
        "butler", _items(), blind_misjudge_rate=1.5).error_code == ERR_INVALID_ARGS
    assert metrics_op.metrics_record(
        "butler", ["不是对象"]).error_code == ERR_INVALID_ARGS
    # 失败不落盘
    assert roles_mod.load_metrics(roles_mod.get_role(roles_dir, "butler")) == {}


# ---------- ⑧ 越界只读聚合 ----------

def test_violations_readonly_aggregation(roles_dir, card):
    roles_mod.append_violation(roles_dir, "butler", "service_tone", "a", now="2026-10-30T01:00:00Z")
    roles_mod.append_violation(roles_dir, "butler", "identity_self_exposure", "b", now="2026-10-30T01:30:00Z")
    roles_mod.append_violation(roles_dir, "butler", "service_tone", "c", now="2026-10-29T23:00:00Z")

    res = metrics_op.metrics_get("butler", now=NOW)

    assert res.ok is True
    v = res.data["violations"]
    assert v["total"] == 3
    assert v["today"] == 2                        # 以 now 的 UTC 日期为准
    assert v["by_kind"] == {"service_tone": 2, "identity_self_exposure": 1}
    # 只读聚合不落盘
    assert roles_mod.load_metrics(roles_mod.get_role(roles_dir, "butler")) == {}


# ---------- ⑨ HTTP 端点接线 ----------

@pytest.fixture
def app(conns, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


def test_http_metrics_endpoints(app, roles_dir, card):
    client = TestClient(app)

    r = client.get("/v1/admin/roles/butler/metrics", headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"] is None and body["latest"] is None
    assert body["runs"] == []
    assert body["violations"] == {"total": 0, "today": 0, "by_kind": {}}

    r = client.post(
        "/v1/admin/roles/butler/metrics",
        json={"items": _items(), "label": "http-round", "set_as_baseline": True,
              "blind_misjudge_rate": 0.3, "meta": {"variant": "prod"}},
        headers=AGENT_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["blind_misjudge_rate"] == 0.3

    body = client.get("/v1/admin/roles/butler/metrics", headers=AGENT_HEADERS).json()
    assert body["baseline"]["label"] == "http-round"
    assert body["baseline"]["blind_misjudge_rate"] == 0.3
    assert body["baseline"]["meta"] == {"variant": "prod"}
    assert body["latest"]["n"] == 3
    assert len(body["latest"]["items"]) == 3      # 逐题原文可读回（供区分度脚本）

    r = client.get("/v1/admin/roles/ghost/metrics", headers=AGENT_HEADERS)
    assert r.status_code == 404
