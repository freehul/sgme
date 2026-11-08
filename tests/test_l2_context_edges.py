"""W03-W05 / W08：L2 单元上下文（_l2_unit）与 belongs_to / causes 边写入。

W01 契约 docs/design/SGME2.0-上下文归属与因果边-v0.1.md §2-§4 的落地验收：

- 单元元数据 ephemeral：主干与侧事实共享 unit_key、角色正确、**绝不落 memories**；
- 映射：归一文本 → 最终 memory_id（成功 / 失败保留 l2_facts_json + WARNING / 同文本歧义优先 main）；
- 四动作：store（含空候选短路）、skip、update、merge（含退化 store）都在最终
  memory_id 确定后按单元建边；skip / quarantine 不建边；
- 降级直存 _fallback_direct_store 同样建边（不得绕过）；
- 幂等：重跑不重复（edge_id = from::to::relation）；
- 生命周期：archive_memory 统一清边（无悬挂边）、purge 清边、supersession 不产生悬挂。
"""
from __future__ import annotations

import json
import logging
import types

import httpx
import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import edge_dao, memory_dao
from sgme.engine import l2_edges, l3_adjudicate, pipeline
from sgme.engine.l2_extract import parse_scene_output, records_to_items

UNIT = l2_edges.UNIT_META_KEY

MAIN_TEXT = "用户反馈提交按钮黑字看不清，要求改成白色"
SIDE_TEXT = "用户偏好深色底配白字"


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture(autouse=True)
def _vector_enabled(monkeypatch):
    """向量端点 mock（与 tests/test_l3_adjudicate.py 同口径）。

    出厂基线 search.vector.enabled=false + prescreen.fallback=skip_conflict，
    embed 不可达时 L1.5 会短路跳过冲突检测——本文件的四动作用例必须真的走裁决路径。
    """
    from sgme.data.search import vector as vector_mod

    monkeypatch.setattr(
        vector_mod, "embed", lambda text, cfg, client=None: [1.0, 0.0, 0.0, 0.0]
    )


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


# ---------- helpers ----------

def _dims(cfg, ids=("preferences", "status", "habits")):
    """从真实注册表取若干维度（_backfill_ttl / scene_key 要按注册表查 TTL）。"""
    out = []
    for d in cfg["dimensions"]:
        if d["id"] in ids:
            out.append({
                "id": d["id"],
                "display_name": d.get("display_name"),
                "active": 1,
                "time_velocity": d.get("time_velocity"),
            })
    assert out, "测试维度 id 在注册表中不存在"
    return out


def _unit(**kw):
    """一个 2.0 单元：主干 + 一条必拆的偏好侧事实（D-22 偏好必拆）。"""
    base = {
        "main_event": {"content": MAIN_TEXT, "occurred_at": "2026-11-08", "feeling": "不满"},
        "facts": [
            {"type": "偏好", "content": SIDE_TEXT, "subject": "self", "certainty": "confirmed"},
        ],
        "causes": [],
        "trait": None,
        "dimensions": {"primary": "preferences"},
        "route": "memory",
    }
    base.update(kw)
    return [base]


def _records(cfg, unit_objs, *, prefix="fileA#c0"):
    """模型输出 → 提炼链形状的 record 列表（parse → 校验 → 补 dimension_ids）。"""
    text = json.dumps(unit_objs, ensure_ascii=False)
    items = records_to_items(
        parse_scene_output(text, _dims(cfg), unit_key_prefix=prefix), _dims(cfg)
    )
    out = []
    for item in items:
        rec = dict(item)
        rec["dimension_ids"] = list(item["dimensions"])
        rec["file_id"] = "fileA"
        out.append(rec)
    return out


def _rec(content, memory_id, role, *, unit_key="u:0", main_content=MAIN_TEXT,
         causes=None, facts=None):
    """手工构造一条带单元上下文的已落库记录（边表无外键，可直接用假 id 测映射）。"""
    return {
        "content": content,
        "memory_id": memory_id,
        UNIT: {
            "unit_key": unit_key,
            "role": role,
            "main_content": main_content,
            "causes": list(causes or []),
            "facts": list(facts or []),
        },
    }


def _mock_llm_client(decisions: list[dict]) -> httpx.Client:
    text = json.dumps(decisions, ensure_ascii=False)

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _insert_memory(mem_conn, content, dim_ids, created_at="2026-01-01T00:00:00Z"):
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=dim_ids,
        created_at=created_at, updated_at=created_at,
    )


def _edge_triples(conn):
    return {
        (e["relation"], e["from_id"], e["to_id"])
        for e in edge_dao.list_edges(conn, source=l2_edges.EDGE_SOURCE, limit=1000)
    }


def _warn_msgs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ---------- 1. 文本归一（契约 §3.1） ----------

def test_normalize_unit_text_contract():
    n = l2_edges.normalize_unit_text
    assert n("  Hello\t\n World 。 ") == "hello world"      # 空白折叠 + 去标点 + ASCII 小写
    assert n("「深色底黑字看不清。」") == "深色底黑字看不清"   # 中文成对引号/句号剥离
    assert n("Button, Weird;") == "button, weird"            # 只剥首尾，不动中间标点
    assert n(None) == "" and n("") == "" and n("   ") == ""
    # 仅 ASCII 小写：非 ASCII 大小写（İ）不得被 .lower() 改写，否则同一文本两处归一不一致
    assert n("İSTANBUL") == "İstanbul"


# ---------- 2. 单元元数据（ephemeral，W01 §2） ----------

def test_flatten_unit_attaches_unit_meta(cfg):
    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    assert len(recs) == 2
    main, side = recs
    assert main[UNIT]["role"] == "main"
    assert side[UNIT]["role"] == "side"
    # 主干与侧事实共享同一 unit_key（= 前缀 + 单元序号）
    assert main[UNIT]["unit_key"] == side[UNIT]["unit_key"] == "fileA#c0:0"
    assert main[UNIT]["main_content"] == MAIN_TEXT
    assert main[UNIT]["causes"] == [{"from": MAIN_TEXT, "to": SIDE_TEXT}]
    # 侧事实同样带 causes（主干未落库时仍能建因果边，见 test_side_without_stored_main）
    assert side[UNIT]["causes"] == [{"from": MAIN_TEXT, "to": SIDE_TEXT}]


def test_unit_key_indexes_per_unit_and_prefix(cfg):
    unit_objs = _unit() + _unit()
    recs = _records(cfg, unit_objs, prefix="fileB#c2")
    keys = [r[UNIT]["unit_key"] for r in recs]
    assert keys == ["fileB#c2:0", "fileB#c2:0", "fileB#c2:1", "fileB#c2:1"]


def test_parse_scene_output_anon_prefix_is_shared(cfg):
    """无前缀（未传 file_id）时也要保证同一单元共享 unit_key（anon 兜底）。"""
    text = json.dumps(_unit(), ensure_ascii=False)
    recs = parse_scene_output(text, _dims(cfg))
    assert len(recs) == 2
    assert recs[0][UNIT]["unit_key"] == recs[1][UNIT]["unit_key"]
    assert recs[0][UNIT]["unit_key"].startswith("anon-")


def test_unit_meta_never_persisted(cfg, mem_conn):
    """ephemeral 键绝不落 memories（含 l2_facts_json 文本）。"""
    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    mid = l3_adjudicate._store_memory(mem_conn, recs[0], cfg["dimensions"], "fileA:1")
    row = mem_conn.execute("SELECT * FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert UNIT not in row.keys()
    text_fields = {k: row[k] for k in row.keys() if isinstance(row[k], str)}
    assert "_l2_unit" not in json.dumps(text_fields, ensure_ascii=False)
    assert UNIT not in memory_dao.get_memory(mem_conn, mid)
    # 主干仍正常落库（剥离不误伤）
    assert row["content"] == MAIN_TEXT


# ---------- 3. 映射成功 / 失败 / 歧义（契约 §3） ----------

def test_write_unit_edges_success(mem_conn):
    recs = [
        _rec(MAIN_TEXT, "m-main", "main", causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]),
        _rec(SIDE_TEXT, "m-side", "side", causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]),
    ]
    stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["units"] == 1 and stats["records"] == 2
    assert stats["belongs_to"] == 1 and stats["causes"] == 1
    assert stats["errors"] == 0
    # 方向：side(from) → main(to)；因果：from(因) → to(果)
    assert _edge_triples(mem_conn) == {
        ("belongs_to", "m-side", "m-main"),
        ("causes", "m-main", "m-side"),
    }
    for edge in edge_dao.list_edges(mem_conn):
        assert edge["source"] == "l2_extract"
        assert edge["weight"] == 1.0


def test_write_unit_edges_idempotent(mem_conn):
    recs = [
        _rec(MAIN_TEXT, "m-main", "main", causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]),
        _rec(SIDE_TEXT, "m-side", "side"),
    ]
    l2_edges.write_unit_edges(mem_conn, recs)
    l2_edges.write_unit_edges(mem_conn, recs)  # 重跑（同 memory_id）
    assert edge_dao.count_edges(mem_conn, l2_edges.EDGE_SOURCE) == 2
    assert l2_edges.count_unit_edges(mem_conn) == {"belongs_to": 1, "causes": 1}


def test_write_unit_edges_unmapped_keeps_json_and_warns(mem_conn, caplog):
    """causes 端点未在同一单元内映射 → 不建边 + WARNING 含 unit_key 与文本（不猜）。"""
    recs = [
        _rec(MAIN_TEXT, "m-main", "main",
             causes=[{"from": MAIN_TEXT, "to": "模型编出来的后果"}]),
        _rec(SIDE_TEXT, "m-side", "side"),
    ]
    with caplog.at_level(logging.WARNING, logger="sgme.engine.l2_edges"):
        stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["causes"] == 0 and stats["causes_unmapped"] == 1
    assert ("causes", "m-main", "m-side") not in _edge_triples(mem_conn)
    msgs = " | ".join(_warn_msgs(caplog))
    assert "causes 边未建" in msgs and "u:0" in msgs and "模型编出来的后果" in msgs


def test_write_unit_edges_ambiguity_side_wins_over_main(mem_conn, caplog):
    """同归一文本对应多条记忆 → 按 side > fact > main 取（W05b：防因果连到主干摘要）。"""
    recs = [
        _rec("按钮改成白字", "m-main", "main", main_content="按钮改成白字",
             causes=[{"from": "按钮改成白字", "to": "对比度不足"}]),
        _rec("按钮改成白字。", "m-side", "side", main_content="按钮改成白字"),
        _rec("对比度不足", "m-cause", "side", main_content="按钮改成白字"),
    ]
    with caplog.at_level(logging.WARNING, logger="sgme.engine.l2_edges"):
        stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["ambiguous"] >= 1
    # side 优先：因果端点连到侧事实自己的记忆，而不是主干
    assert ("causes", "m-side", "m-cause") in _edge_triples(mem_conn)
    assert ("causes", "m-main", "m-cause") not in _edge_triples(mem_conn)
    assert any("side > fact > main" in m for m in _warn_msgs(caplog))


def test_write_unit_edges_self_cause_skipped(mem_conn):
    recs = [
        _rec(MAIN_TEXT, "m-main", "main", causes=[{"from": MAIN_TEXT, "to": MAIN_TEXT}]),
        _rec(SIDE_TEXT, "m-side", "side"),
    ]
    stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["causes"] == 0 and stats["causes_self"] == 1
    assert ("causes", "m-main", "m-main") not in _edge_triples(mem_conn)


def test_write_unit_edges_ignores_records_without_meta(mem_conn):
    """1.x 路径（无 _l2_unit）/ 无 memory_id 的记录不建边、不报错。"""
    stats = l2_edges.write_unit_edges(mem_conn, [
        {"content": "1.x 记忆", "memory_id": "m1"},
        _rec(SIDE_TEXT, None, "side"),          # 未落库（skip/隔离/主事件缺失）
    ])
    assert stats["no_meta"] == 1 and stats["no_id"] == 1
    assert edge_dao.count_edges(mem_conn) == 0


def test_side_without_stored_main_keeps_causes(mem_conn, caplog):
    """主事件未落库 → 侧事实不建父边（WARNING），但 causes 内可映射的仍建。"""
    causes = [{"from": SIDE_TEXT, "to": "用户讨厌低对比度"}]
    recs = [
        _rec(MAIN_TEXT, None, "main", causes=causes),        # 主事件被判 skip
        _rec(SIDE_TEXT, "m-s1", "side", causes=causes),
        _rec("用户讨厌低对比度", "m-s2", "side", causes=causes),
    ]
    with caplog.at_level(logging.WARNING, logger="sgme.engine.l2_edges"):
        stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["belongs_to"] == 0 and stats["belongs_to_skipped"] == 2
    assert ("causes", "m-s1", "m-s2") in _edge_triples(mem_conn)
    assert any("belongs_to 未建" in m for m in _warn_msgs(caplog))


def test_write_unit_edges_error_is_fail_open(mem_conn, monkeypatch, caplog):
    """边写入异常只 WARNING，绝不向上抛（契约 §1.4 失败不阻塞落库）。"""
    def boom(*a, **k):
        raise RuntimeError("edge boom")

    monkeypatch.setattr(l2_edges.edge_dao, "create_edge", boom)
    recs = [_rec(MAIN_TEXT, "m-main", "main"), _rec(SIDE_TEXT, "m-side", "side")]
    with caplog.at_level(logging.WARNING, logger="sgme.engine.l2_edges"):
        stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["errors"] == 1
    assert any("单元边写入异常" in m for m in _warn_msgs(caplog))


def test_list_edges_for_memory_filters(mem_conn):
    edge_dao.create_edge(mem_conn, "a", "b", "belongs_to", source="l2_extract")
    edge_dao.create_edge(mem_conn, "b", "a", "causes", source="l2_extract")
    edge_dao.create_edge(mem_conn, "a", "c", "similar", weight=0.8, source="l1_conflict")
    mem_conn.commit()
    both = edge_dao.list_edges_for_memory(mem_conn, "a")
    assert {e["relation"] for e in both} == {"belongs_to", "causes", "similar"}
    only_l2 = edge_dao.list_edges_for_memory(mem_conn, "a", source="l2_extract")
    assert {e["source"] for e in only_l2} == {"l2_extract"}
    rels = edge_dao.list_edges_for_memory(
        mem_conn, "a", relations=("belongs_to",), source=["l2_extract"])
    assert [e["relation"] for e in rels] == ["belongs_to"]
    assert edge_dao.list_edges_for_memory(mem_conn, "nobody") == []
    assert edge_dao.list_edges_for_memory(mem_conn, "") == []
    assert len(edge_dao.list_edges_for_memory(mem_conn, "a", limit=1)) == 1


# ---------- 4. 四动作：store（空候选短路）/ skip / update / merge ----------

def test_store_short_circuit_builds_edges(cfg, mem_conn):
    """空候选池短路（不调 LLM）路径：落库后按单元建边。"""
    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    res = l3_adjudicate.resolve_conflicts(recs, mem_conn, cfg, source_ref="fileA:1")
    assert len(res.stored) == 2 and res.skipped == []
    assert res.unit_edges["belongs_to"] == 1 and res.unit_edges["causes"] == 1
    assert l2_edges.count_unit_edges(mem_conn) == {"belongs_to": 1, "causes": 1}
    main_id, side_id = recs[0]["memory_id"], recs[1]["memory_id"]
    assert _edge_triples(mem_conn) == {
        ("belongs_to", side_id, main_id),
        ("causes", main_id, side_id),
    }


def test_skip_action_writes_no_edges(cfg, mem_conn):
    cand = _insert_memory(mem_conn, "用户以前偏好黑色按钮", ["preferences"])
    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    client = _mock_llm_client([
        {"new_memory_index": 0, "candidate_ids": [cand], "action": "skip", "reason": "重复"},
        {"new_memory_index": 1, "candidate_ids": [], "action": "skip", "reason": "重复"},
    ])
    res = l3_adjudicate.resolve_conflicts(
        recs, mem_conn, cfg, client=client, source_ref="fileA:1")
    assert res.stored == [] and len(res.skipped) == 2
    assert l2_edges.count_unit_edges(mem_conn) == {"belongs_to": 0, "causes": 0}
    assert res.unit_edges["no_id"] == 2   # 没有最终 memory_id → 不建边


def test_update_action_uses_new_id_and_clears_old_edges(cfg, mem_conn):
    cand = _insert_memory(mem_conn, "用户以前偏好黑色按钮", ["preferences"])
    other = _insert_memory(mem_conn, "其他不相关记忆", ["preferences"])
    # 旧候选上先挂一条边（模拟历史边）——归档后必须一起消失，不留悬挂
    edge_dao.create_edge(mem_conn, cand, other, "belongs_to", source=l2_edges.EDGE_SOURCE)
    mem_conn.commit()

    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    client = _mock_llm_client([
        {"new_memory_index": 0, "candidate_ids": [cand], "action": "update", "reason": "更具体"},
        {"new_memory_index": 1, "candidate_ids": [], "action": "store"},
    ])
    res = l3_adjudicate.resolve_conflicts(
        recs, mem_conn, cfg, client=client, source_ref="fileA:1")
    assert len(res.updated) == 1 and cand in res.archived
    new_main = res.updated[0]
    side_id = recs[1]["memory_id"]
    assert res.stored == [side_id]
    # 归档：旧行消失、旧 id 的边清零
    assert memory_dao.get_memory(mem_conn, cand) is None
    assert edge_dao.list_edges_for_memory(mem_conn, cand) == []
    # 新 id 是边的端点（side → 新主干；因果 from 新主干 → 侧事实）
    assert _edge_triples(mem_conn) == {
        ("belongs_to", side_id, new_main),
        ("causes", new_main, side_id),
    }


def test_merge_action_uses_new_id(cfg, mem_conn):
    cand = _insert_memory(mem_conn, "用户偏好深色按钮", ["preferences"])
    other = _insert_memory(mem_conn, "其他不相关记忆", ["preferences"])
    edge_dao.create_edge(mem_conn, cand, other, "belongs_to", source=l2_edges.EDGE_SOURCE)
    mem_conn.commit()

    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    client = _mock_llm_client([
        {"new_memory_index": 0, "candidate_ids": [cand], "action": "merge",
         "merged_content": "深色底黑字对比度不足 → 按钮统一白字配实色底",
         "reason": "同一规则的不同说法"},
        {"new_memory_index": 1, "candidate_ids": [], "action": "store"},
    ])
    res = l3_adjudicate.resolve_conflicts(
        recs, mem_conn, cfg, client=client, source_ref="fileA:1")
    assert len(res.merged) == 1 and cand in res.archived
    new_main = res.merged[0]
    side_id = recs[1]["memory_id"]
    assert memory_dao.get_memory(mem_conn, cand) is None
    assert edge_dao.list_edges_for_memory(mem_conn, cand) == []
    assert _edge_triples(mem_conn) == {
        ("belongs_to", side_id, new_main),
        ("causes", new_main, side_id),
    }


def test_update_without_candidates_degrades_to_store(cfg, mem_conn):
    """update 退化（无 candidate_ids）→ 走 store，边照建。"""
    _insert_memory(mem_conn, "用户以前偏好黑色按钮", ["preferences"])
    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    client = _mock_llm_client([
        {"new_memory_index": 0, "candidate_ids": [], "action": "update"},
        {"new_memory_index": 1, "candidate_ids": [], "action": "store"},
    ])
    res = l3_adjudicate.resolve_conflicts(
        recs, mem_conn, cfg, client=client, source_ref="fileA:1")
    assert len(res.stored) == 2 and res.updated == []
    assert l2_edges.count_unit_edges(mem_conn) == {"belongs_to": 1, "causes": 1}


def test_edge_failure_does_not_block_l15_store(cfg, mem_conn, monkeypatch):
    """边写入异常时记忆照常落库（L1.5 返回正常 store 计数）。"""
    def boom(*a, **k):
        raise RuntimeError("edge boom")

    monkeypatch.setattr(l2_edges.edge_dao, "create_edge", boom)
    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    res = l3_adjudicate.resolve_conflicts(recs, mem_conn, cfg, source_ref="fileA:1")
    assert len(res.stored) == 2
    assert res.unit_edges["errors"] >= 1
    for mid in res.stored:
        assert memory_dao.get_memory(mem_conn, mid) is not None


# ---------- 5. 降级直存（pipeline._fallback_direct_store） ----------

def test_fallback_direct_store_builds_edges(cfg, mem_conn):
    recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
    fr = types.SimpleNamespace(memories=recs, file_id="fileA")
    stats = pipeline._fallback_direct_store(fr, mem_conn, cfg, ["fileA:1", "fileA:2"])
    assert stats["fallback"] is True and stats["stored"] == 2
    assert stats["unit_edges"]["belongs_to"] == 1
    assert l2_edges.count_unit_edges(mem_conn) == {"belongs_to": 1, "causes": 1}
    main_id, side_id = recs[0]["memory_id"], recs[1]["memory_id"]
    assert _edge_triples(mem_conn) == {
        ("belongs_to", side_id, main_id),
        ("causes", main_id, side_id),
    }
    # 重跑：同源同内容 → 幂等跳过，边不重复
    stats2 = pipeline._fallback_direct_store(fr, mem_conn, cfg, ["fileA:1", "fileA:2"])
    assert stats2["skipped"] == 2 and stats2["stored"] == 0
    assert edge_dao.count_edges(mem_conn, l2_edges.EDGE_SOURCE) == 2


def test_fallback_direct_store_never_persists_unit_meta(cfg, mem_conn):
    recs = _records(cfg, _unit())
    fr = types.SimpleNamespace(memories=recs, file_id="fileA")
    pipeline._fallback_direct_store(fr, mem_conn, cfg, ["fileA:1", "fileA:2"])
    for rec in recs:
        row = mem_conn.execute(
            "SELECT * FROM memories WHERE memory_id=?", (rec["memory_id"],)).fetchone()
        assert UNIT not in row.keys()


# ---------- 6. 生命周期：归档/删除统一清边（W01 §4） ----------

def test_archive_memory_clears_edges_both_directions(mem_conn):
    a = _insert_memory(mem_conn, "旧规则 A", ["preferences"])
    b = _insert_memory(mem_conn, "新规则 B", ["preferences"])
    c = _insert_memory(mem_conn, "其他 C", ["preferences"])
    edge_dao.create_edge(mem_conn, a, b, "belongs_to", source=l2_edges.EDGE_SOURCE)
    edge_dao.create_edge(mem_conn, b, a, "causes", source=l2_edges.EDGE_SOURCE)
    edge_dao.create_edge(mem_conn, b, c, "belongs_to", source=l2_edges.EDGE_SOURCE)
    mem_conn.commit()

    assert memory_dao.archive_memory(mem_conn, a, superseded_by=b) is True
    # from_id / to_id 两侧的边都没了
    assert edge_dao.list_edges_for_memory(mem_conn, a) == []
    # 与 a 无关的边原样保留
    assert [e["edge_id"] for e in edge_dao.list_edges_for_memory(mem_conn, b)] == [
        f"{b}::{c}::belongs_to"]
    assert edge_dao.count_edges(mem_conn) == 1


def test_update_merge_leave_no_dangling_edges(cfg, mem_conn):
    """update / merge 归档旧候选后，全库不存在指向已不存在记忆的边。"""
    cand_u = _insert_memory(mem_conn, "用户偏好黑色按钮", ["preferences"])
    cand_m = _insert_memory(mem_conn, "用户偏爱深色系按钮", ["preferences"])
    edge_dao.create_edge(mem_conn, cand_u, cand_m, "belongs_to", source=l2_edges.EDGE_SOURCE)
    mem_conn.commit()

    for action, cand in (("update", cand_u), ("merge", cand_m)):
        recs = _records(cfg, _unit(causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}]))
        decision = {"new_memory_index": 0, "candidate_ids": [cand], "action": action,
                    "reason": "t"}
        if action == "merge":
            decision["merged_content"] = "深色底黑字对比度不足 → 按钮统一白字配实色底"
        client = _mock_llm_client([decision,
                                   {"new_memory_index": 1, "candidate_ids": [],
                                    "action": "store"}])
        res = l3_adjudicate.resolve_conflicts(
            recs, mem_conn, cfg, client=client, source_ref=f"fileA:{action}")
        assert (res.updated or res.merged), action
        dangling = mem_conn.execute(
            "SELECT COUNT(*) AS c FROM memory_edges e "
            "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id = e.from_id) "
            "   OR NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id = e.to_id)"
        ).fetchone()["c"]
        assert dangling == 0, action


def test_purge_memory_still_clears_edges(mem_conn):
    a = _insert_memory(mem_conn, "待清除 A", ["preferences"])
    b = _insert_memory(mem_conn, "保留 B", ["preferences"])
    edge_dao.create_edge(mem_conn, a, b, "belongs_to", source=l2_edges.EDGE_SOURCE)
    mem_conn.commit()
    memory_dao.purge_memory(mem_conn, a, triggered_by="test", reason="回归")
    assert edge_dao.list_edges_for_memory(mem_conn, a) == []


def test_supersession_linkage_leaves_no_dangling_edges(mem_conn):
    """supersession 只改 lifecycle（行仍在）→ 边可解析；后续真归档才清边。"""
    old = _insert_memory(mem_conn, "AIRDT 左栏为单列布局", ["preferences"])
    other = _insert_memory(mem_conn, "其他不相关记忆", ["preferences"])
    edge_dao.create_edge(mem_conn, old, other, "belongs_to", source=l2_edges.EDGE_SOURCE)
    mem_conn.commit()

    new_content = "AIRDT 左栏改为上下两栏（取代单列布局）"
    new = _insert_memory(mem_conn, new_content, ["preferences"])
    rejected = l3_adjudicate.apply_supersession_linkage(mem_conn, [{
        "memory_id": new, "content": new_content,
        "supersedes": ["AIRDT 左栏为单列布局"], "dimension_ids": ["preferences"],
    }])
    assert rejected == [old]
    row = mem_conn.execute(
        "SELECT lifecycle, superseded_by FROM memories WHERE memory_id=?", (old,)).fetchone()
    assert row["lifecycle"] == "archived" and row["superseded_by"] == new
    # 行还在 → 边不悬挂（两个端点都能在 memories 里解析）
    edges = edge_dao.list_edges_for_memory(mem_conn, old)
    assert len(edges) == 1
    dangling = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM memory_edges e "
        "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id = e.from_id) "
        "   OR NOT EXISTS (SELECT 1 FROM memories m WHERE m.memory_id = e.to_id)"
    ).fetchone()["c"]
    assert dangling == 0
    # 真归档时统一清边
    memory_dao.archive_memory(mem_conn, old, superseded_by=new)
    assert edge_dao.list_edges_for_memory(mem_conn, old) == []


# ---------- 7. 提炼链接线：_l2_unit 必须活过 normalize（白名单回归） ----------

def test_parse_extraction_output_carries_file_prefix(cfg):
    """解析层的前缀 = bucket_key(file_id) + 块序（契约 §2 的 file_id 兜底）。"""
    from sgme.engine import l2_extraction
    from sgme.prompts import BucketCtx

    prefix = l2_extraction._unit_key_prefix(BucketCtx(bucket_key="fileQ"), 2)
    assert prefix == "fileQ#c2"
    assert l2_extraction._unit_key_prefix(None) is None

    text = json.dumps(_unit(), ensure_ascii=False)
    recs = l2_extraction._parse_extraction_output(text, _dims(cfg), unit_key_prefix=prefix)
    assert {r[UNIT]["unit_key"] for r in recs} == {"fileQ#c2:0"}


def test_normalize_memories_keeps_unit_meta(cfg, mem_conn):
    """refine.normalize_memories 的字段白名单不能吃掉 _l2_unit（否则生产静默丢边）。"""
    from sgme.engine import refine

    text = json.dumps(_unit(), ensure_ascii=False)
    raw = records_to_items(
        parse_scene_output(text, _dims(cfg), unit_key_prefix="fileZ#c0"), _dims(cfg)
    )
    normalized, _stats = refine.normalize_memories(
        raw, cfg["dimensions"], mem_conn,
        file_id="fileZ", seq_ts={}, sunk_refs=[],
    )
    assert len(normalized) == 2
    assert {r[UNIT]["unit_key"] for r in normalized} == {"fileZ#c0:0"}
    assert {r[UNIT]["role"] for r in normalized} == {"main", "side"}


def test_unit_meta_key_absent_from_extra_whitelists():
    """落库白名单必须**不含** ephemeral 键（L2 上下文只走内存）。"""
    assert UNIT not in l3_adjudicate._EXTRA_KEYS
    assert UNIT not in memory_dao._EXTRA_COLUMNS

# ---------- 8. W05b：unit facts 登记（非 split → main / split → side / 优先级） ----------

STATUS_TEXT = "用户对按钮对比度不满"


def _unit_with_facts(**kw):
    """主干 + 偏好侧事实（split）+ 状态事实（不 split）；causes 引用两段 fact 原文。"""
    base = {
        "main_event": {"content": MAIN_TEXT, "occurred_at": "2026-11-08"},
        "facts": [
            {"type": "偏好", "content": SIDE_TEXT, "subject": "self", "certainty": "confirmed"},
            {"type": "状态", "content": STATUS_TEXT, "subject": "self",
             "certainty": "confirmed"},
        ],
        "causes": [{"from": STATUS_TEXT, "to": SIDE_TEXT}],
        "dimensions": {"primary": "preferences"},
        "route": "memory",
    }
    base.update(kw)
    return [base]


def test_flatten_unit_meta_includes_facts(cfg):
    """_l2_unit.facts 同时登记 kept_facts 与拆出的 side facts，并标 split。"""
    recs = _records(cfg, _unit_with_facts())
    assert len(recs) == 2  # 主干 + 偏好侧事实（状态留主干不拆）
    facts = recs[0][UNIT]["facts"]
    assert facts == [
        {"content": SIDE_TEXT, "type": "偏好", "split": True},
        {"content": STATUS_TEXT, "type": "状态", "split": False},
    ]
    # 侧事实记录共享同一份 facts（建边按单元取，不依赖哪条承载）
    assert recs[1][UNIT]["facts"] == facts


def test_non_split_fact_maps_to_main(cfg, mem_conn):
    """非 split fact 作 cause 端点 → 该端点落到主干记忆（它没有独立记忆）。"""
    recs = _records(cfg, _unit_with_facts())
    res = l3_adjudicate.resolve_conflicts(recs, mem_conn, cfg, source_ref="fileA:1")
    assert len(res.stored) == 2
    main_id, side_id = recs[0]["memory_id"], recs[1]["memory_id"]
    assert res.unit_edges["facts_mapped"] == 1
    # causes: 状态事实(from, → 主干) → 偏好事实(to, → 自己的侧事实)
    assert _edge_triples(mem_conn) == {
        ("belongs_to", side_id, main_id),
        ("causes", main_id, side_id),
    }


def test_split_fact_maps_to_side_not_main(mem_conn):
    """split fact 不登记 role=fact：同文本只归它自己的侧事实记录。"""
    recs = [
        _rec(MAIN_TEXT, "m-main", "main", main_content=MAIN_TEXT,
             causes=[{"from": MAIN_TEXT, "to": SIDE_TEXT}],
             facts=[{"content": SIDE_TEXT, "type": "偏好", "split": True}]),
        _rec(SIDE_TEXT, "m-side", "side", main_content=MAIN_TEXT,
             facts=[{"content": SIDE_TEXT, "type": "偏好", "split": True}]),
    ]
    stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["facts_mapped"] == 0
    assert ("causes", "m-main", "m-side") in _edge_triples(mem_conn)


def test_priority_side_over_fact(mem_conn, caplog):
    """同一文本既是非 split fact（→主干）又是侧事实（→自身记忆）→ 取 side。"""
    recs = [
        _rec(MAIN_TEXT, "m-main", "main", main_content=MAIN_TEXT,
             causes=[{"from": MAIN_TEXT, "to": STATUS_TEXT}],
             facts=[{"content": STATUS_TEXT, "type": "状态", "split": False}]),
        # 模型在两处写了同一句：一条侧事实与那条非 split fact 同文本
        _rec(STATUS_TEXT, "m-side", "side", main_content=MAIN_TEXT,
             facts=[{"content": STATUS_TEXT, "type": "状态", "split": False}]),
    ]
    with caplog.at_level(logging.WARNING, logger="sgme.engine.l2_edges"):
        stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["ambiguous"] >= 1
    # 若取 fact/main 会自环（同一 id）→ causes_self 被跳过；取 side 才有这条边
    assert stats["causes"] == 1 and stats["causes_self"] == 0
    assert ("causes", "m-main", "m-side") in _edge_triples(mem_conn)
    assert any("side > fact > main" in m for m in _warn_msgs(caplog))


def test_facts_not_registered_when_main_missing(mem_conn):
    """主干未落库 → facts 无归属，不登记（不猜）；端点随之 unmapped。"""
    recs = [
        _rec(MAIN_TEXT, None, "main",
             facts=[{"content": STATUS_TEXT, "type": "状态", "split": False}],
             causes=[{"from": STATUS_TEXT, "to": SIDE_TEXT}]),
        # 生产里单元内每条记录都带同一份 causes（见 _flatten_unit），这里同样给上
        _rec(SIDE_TEXT, "m-side", "side", main_content=MAIN_TEXT,
             facts=[{"content": STATUS_TEXT, "type": "状态", "split": False}],
             causes=[{"from": STATUS_TEXT, "to": SIDE_TEXT}]),
    ]
    stats = l2_edges.write_unit_edges(mem_conn, recs)
    assert stats["facts_mapped"] == 0
    assert stats["causes"] == 0 and stats["causes_unmapped"] == 1


def test_facts_mapping_idempotent(cfg, mem_conn):
    recs = _records(cfg, _unit_with_facts())
    l3_adjudicate.resolve_conflicts(recs, mem_conn, cfg, source_ref="fileA:1")
    before = edge_dao.count_edges(mem_conn, l2_edges.EDGE_SOURCE)
    l2_edges.write_unit_edges(mem_conn, recs)  # 同一批重跑（memory_id 已回写）
    assert edge_dao.count_edges(mem_conn, l2_edges.EDGE_SOURCE) == before
