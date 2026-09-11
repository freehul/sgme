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
