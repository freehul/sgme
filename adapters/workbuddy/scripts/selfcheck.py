# -*- coding: utf-8 -*-
"""
selfcheck.py — SGME × WorkBuddy 接入自检（能力矩阵 + 连通性）
=============================================================

两层自检，全部可重跑：

  ① 静态能力矩阵（离线）：核对 41 个 MCP 基准能力是否**方法层 + CLI 层**双覆盖
     （权威基准：sgme/mcp_server.py 工具清单）
  ② 连通性实测（在线）：HTTP 只读项 + MCP 只读项 + 一次 append 接入心跳

无其它副作用：不调写侧/管理类能力（skill_put/delete/rename、config_update、
signal_clear、wiki_evolve_trigger、role_active_set 等有真实副作用，只做静态覆盖核对）。
输出不打印密钥，也不回显记忆正文（只报条数）。

用法：
  python selfcheck.py                # 全量自检（HTTP 层 + MCP 层）
  python selfcheck.py --skip-mcp     # 只检 HTTP 层（未装 mcp 库时）
  python selfcheck.py --no-append    # 不写接入心跳
  python selfcheck.py --static-only  # 只跑静态能力矩阵（离线，不连服务）
"""
from __future__ import annotations

import argparse
import sys

from sgme_client import (
    ADMIN_KEY_ENV,
    AGENT_ID,
    BASELINE_TOOLS,
    SGME,
    SGMEError,
    capability_report,
)


def _count(resp) -> str:
    """从响应里取规模数字（不泄露正文）。"""
    if isinstance(resp, dict):
        for k in ("total", "count", "len"):
            if isinstance(resp.get(k), int):
                return f"{resp[k]} 条"
        for k in ("results", "items", "memories", "pages", "skills", "signals", "events"):
            if isinstance(resp.get(k), list):
                return f"{len(resp[k])} 条"
        return "ok"
    if isinstance(resp, list):
        return f"{len(resp)} 条"
    return "ok"


def run_static() -> tuple[list[tuple[str, bool, str]], bool]:
    """静态能力矩阵核对：41 基准能力的方法层与 CLI 层覆盖。"""
    rows = capability_report()
    bad = [r for r in rows if not (r["method_ok"] and r["cli_ok"])]
    detail = f"{len(rows) - len(bad)}/{len(rows)} 基准能力双覆盖（方法层 + CLI 层）"
    out = [("静态 ① 基准能力矩阵（41 个）", not bad, detail)]
    for r in bad:
        out.append((f"     ↳ 缺失 {r['tool']}", False,
                    f"方法={r['method']}({r['method_ok']}) CLI={r['cli']}({r['cli_ok']})"))
    return out, not bad


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SGME × WorkBuddy 接入自检")
    p.add_argument("--skip-mcp", action="store_true", help="跳过 MCP 层检查")
    p.add_argument("--no-append", action="store_true", help="不写接入心跳")
    p.add_argument("--static-only", action="store_true", help="只跑静态能力矩阵（离线）")
    a = p.parse_args(argv)

    results, static_ok = run_static()

    if not a.static_only:
        try:
            c = SGME()
        except SGMEError as e:
            print(f"❌ {e}")
            return 2

        info = c.describe()
        print(f"端点数：HTTP {info['http_url']} ｜ MCP {info['mcp_url']}"
              f"（来源：{info['address_source']}）")
        print(f"agent_id：{AGENT_ID}（服务端溯源打标用）")
        print(f"密钥：agent key {'已取到' if info['agent_key_set'] else '未取到'}"
              f"（来源：{info['agent_key_source'] or '—'}）"
              f" ｜ {ADMIN_KEY_ENV}={'已设置' if info['admin_key_set'] else '未设置'}")
        if "兜底" in (info.get("agent_key_source") or ""):
            print("  ⚠️ 密钥走通用兜底路径：本机 SGME_AGENT_KEY 可能属其它 agent，"
                  "建议改用 ~/.workbuddy/mcp.json 或 SGME_WORKBUDDY_KEY")
        print()

        def run(name, fn):
            try:
                results.append((name, True, fn()))
            except SGMEError as e:
                results.append((name, False, str(e)[:200]))
            except Exception as e:  # noqa: BLE001 — 自检必须报出任何异常，不能中断整轮
                results.append((name, False, f"{type(e).__name__}: {e}"[:200]))

        # ---- HTTP 层（只读 + 一次心跳写入） ----
        run("② health（服务可达）",
            lambda: "status=" + str(c.health().get("status")))
        run("③ search（记忆检索）", lambda: _count(c.search("接入自检", limit=1)))
        run("④ inject（画像注入）", lambda: _count(c.inject("daily")))
        run("⑤ skill_search（技能检索）", lambda: _count(c.skill_search("sgme", limit=3)))
        run("⑥ wiki_search（知识库检索）", lambda: _count(c.wiki_search("接入", limit=3)))
        run("⑦ events_pull（事件游标）", lambda: _count(c.events_pull("workbuddy", 5)))

        def _append_heartbeat():
            if a.no_append:
                return "skipped"
            # session_key 必须带 agent 前缀：三个 Skill 型适配器曾共用 "sgme-selfcheck"，
            # 导致心跳互相追加进同一个 L0 文件、agent 归属错乱（B194 实测发现）。
            r = c.append(f"{AGENT_ID}-selfcheck", "接入自检心跳（selfcheck.py）",
                         agent_id=AGENT_ID)
            return str(r.get("status") or r.get("file_id") or "ok")[:40]
        run("⑧ append（L0 写入心跳）", _append_heartbeat)

        # ---- MCP 层（只读） ----
        if not a.skip_mcp:
            run("⑨ MCP stats（引擎统计）", lambda: _count(c.stats()))
            run("⑩ MCP config_get（配置读取）", lambda: _count(c.config_get()))
            run("⑪ MCP refine_status（提炼进度）", lambda: _count(c.refine_status()))
            run("⑫ MCP role_list（角色清单）", lambda: _count(c.role_list()))
            run("⑬ MCP role_active_get（当前角色）", lambda: _count(c.role_active_get()))
            run("⑭ MCP signal_pull（关怀信号）", lambda: _count(c.signal_pull(limit=5)))

    ok = sum(1 for _, s, _ in results if s)
    print(f"SGME × WorkBuddy 接入自检（{ok}/{len(results)} 通过）  基准能力 {len(BASELINE_TOOLS)} 个\n")
    for name, s, detail in results:
        mark = "✅" if s else "❌"
        d = str(detail)[:110].replace("\n", " ")
        print(f"  {mark} {name}" + (f"  ← {d}" if s else ""))
        if not s:
            print(f"      ↳ {d}")
    print("\n失败项按 agent-onboarding §9 常见坑排障；仍失败如实上报，禁止谎称已完成。")
    return 0 if static_ok and ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
