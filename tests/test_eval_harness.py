"""评测台（eval/longmemeval_eval.py）健壮性回归测试。

覆盖两条 2026-09-11 实锤教训：
1. 评测脚本自己的 HTTP 调用必须 trust_env=False —— 宿主机残留的死代理
   （HTTP_PROXY 指向未运行的本机端口）会让批量向量与判分全部 10061 连接被拒，
   而引擎侧（trust_env=False）照常成功，故障表现为「同一进程一半通一半不通」。
2. 断点续跑不得把 error 记录当作「已完成」—— 否则一次网络抖动就让题目被永久跳过。
"""

from __future__ import annotations

import httpx

from eval import longmemeval_eval as lme


def test_no_proxy_client_disables_env_proxy(monkeypatch):
    """评测台统一 HTTP 客户端必须 trust_env=False（防死代理劫持）。"""
    seen: dict = {}

    class FakeClient:
        def __init__(self, **kw):  # noqa: D401
            seen.update(kw)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    lme._no_proxy_client(30.0)

    assert seen.get("trust_env") is False, "评测台 HTTP 客户端必须显式 trust_env=False"
    assert seen.get("timeout") == 30.0


def test_done_qids_excludes_error_records():
    """error 记录不算完成 → resume 会重跑它，避免永久丢题。"""
    records = [
        {"qid": "a", "error": None},
        {"qid": "b", "error": "batch embed 耗尽重试: ..."},
        {"qid": "c"},
        {"qid": None, "error": None},
    ]
    assert lme._done_qids(records) == {"a", "c"}


def test_run_eval_env_import_is_side_effect_free():
    """import 启动器不得有副作用 —— 实测 import 曾直接拉起一次默认臂评测（事故）。"""
    import importlib

    mod = importlib.import_module("eval.run_eval_env")
    assert callable(mod.main), "启动器应把主流程收进 main() 并用 __main__ 守卫"


def test_launcher_overrides_allow_env_switch():
    """装载档切换：同名环境变量优先于默认覆盖。"""
    from eval import run_eval_env as ree

    merged = ree._effective_overrides({"SGME_REFINE_CTX": "32768"})
    assert merged["SGME_REFINE_CTX"] == "32768"
    # 未覆盖项保持默认（提炼与向量都在 PC）
    assert merged["SGME_EMBED_BASE_URL"].startswith("http://192.168.10.130")
    assert merged["SGME_REFINE_BASE_URL"].startswith("http://192.168.10.130")


def test_launcher_strips_proxy_vars():
    """启动器必须清掉代理变量（死代理会让向量/判分全报 10061）。"""
    from eval import run_eval_env as ree

    assert "HTTP_PROXY" in ree.PROXY_VARS and "https_proxy" in ree.PROXY_VARS


# ── refined 臂「会话级聚合」口径（2026-09-12）──────────────────────────────
# 背景：refined 库每场会话产出 8~10 条细粒度记忆，而检索给的是 top-k 条记忆。
# 同一 k 下 refined 臂只覆盖 1~2 场会话，直灌臂覆盖 8 场完整会话 → 不可比。

def test_source_to_sid_strips_segment_suffix():
    """source_ref 形如 '<file_id>:<段号>'，必须剥段号再查映射表。"""
    from eval.longmemeval_eval import _source_to_sid

    f2s = {"fid-a": "sess-1"}
    assert _source_to_sid("fid-a:7", f2s) == "sess-1"
    assert _source_to_sid("fid-a", f2s) == "sess-1"
    assert _source_to_sid("fid-unknown", f2s) == "fid-unknown"  # 兜底返回原值


def test_rank_sessions_by_best_memory_rank():
    """会话按其最好一条记忆的排名排序、去重、截断到 k（一条记忆可来自多场会话）。"""
    from eval.longmemeval_eval import _rank_sessions

    mem_ids = ["m1", "m2", "m3", "m4", "m5"]        # 已按检索排名排列
    mid2sids = {"m1": ["A"], "m2": ["B"], "m3": ["A"], "m4": ["C"], "m5": ["B", "D"]}
    assert _rank_sessions(mem_ids, mid2sids, 3) == ["A", "B", "C"]
    assert _rank_sessions(mem_ids, mid2sids, 1) == ["A"]
    assert _rank_sessions(mem_ids[:2], mid2sids, 5) == ["A", "B"]
    # 多来源记忆：它同时把两场会话顶到前面
    assert _rank_sessions(["m1", "m5"], mid2sids, 5) == ["A", "B", "D"]


def test_select_memories_within_budget_keeps_rank_order():
    """选中会话的记忆按原排名拼上下文，且不超过字符预算。"""
    from eval.longmemeval_eval import _select_memories_within_budget

    mem_ids = ["m1", "m2", "m3", "m4"]
    mid2sids = {"m1": ["A"], "m2": ["B"], "m3": ["A"], "m4": ["C"]}
    texts = {"m1": "a" * 100, "m2": "b" * 100, "m3": "c" * 100, "m4": "d" * 100}
    # 只要 A 会话：m1 + m3，共 200 字符
    assert _select_memories_within_budget(mem_ids, mid2sids, {"A"}, 10_000, texts) == ["m1", "m3"]
    # 预算 150 → 只装得下 m1（200 会超）
    assert _select_memories_within_budget(mem_ids, mid2sids, {"A"}, 150, texts) == ["m1"]
    # 预算为 0 也要保证至少一条（否则上下文为空，等于白跑）
    assert _select_memories_within_budget(mem_ids, mid2sids, {"A"}, 0, texts) == ["m1"]
    # 高排名记忆不属于选中会话时，跳过它、继续用后面的
    assert _select_memories_within_budget(mem_ids, mid2sids, {"B"}, 10_000, texts) == ["m2"]

