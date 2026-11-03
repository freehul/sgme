# -*- coding: utf-8 -*-
"""tests/test_gate_scripts.py：两道门禁脚本的回归测试（SGME 2.0 对齐整改 · T-446）。

覆盖两类门禁的**判定口径**，不是重复跑一遍脚本：

A. scripts/check_adapter_freshness.py（部署态契约）
   - 永久豁免从 scripts/adapter_parity_map.yaml 的 exemptions 段动态读取，
     与 adapter_parity 同源（2026-11-03 前写死两项 → 4 个 skill_candidate_* 假缺口）；
   - 别名只从 aliases 段读取（2026-11-03 前按缩进扫全 YAML，把 exemptions 里
     dsh 的中文理由当工具名，输出出现「缺工具 '同上（…）'」这类脏数据）；
   - 豁免工具不进契约（部署缺它不算缺口）、真缺工具照旧报缺口；
   - --json 结构不变、无 parity map 时退化口径稳定。

B. scripts/verify_decisions.py（决策级对账）
   - 覆盖 >= 40/73、MISMATCH 0、--strict 0（发布门禁可绿）；
   - 判定矩阵：done/partial 断言失败 → MISMATCH；planned 断言全过 → 文档落后
     （strict 下失败、默认只警告）；planned 未过 → 待施工（不算失败）；
   - D-55 回归：guardrail 的 read_mode 不得再被当成迁移配置的切读开关（旧假阳性）；
   - 断言失败消息必须带「文件路径 + 缺失符号」。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts import adapter_parity as ap  # noqa: E402  仅用于「同源」对照
from scripts import check_adapter_freshness as cf  # noqa: E402
from scripts import verify_decisions as vd  # noqa: E402

TOOL_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ===========================================================================
# A. check_adapter_freshness.py
# ===========================================================================

PARITY_WITH_BOTH_SECTIONS = """\
version: 1
baseline:
  file: sgme/mcp_server.py
aliases:
  search:
    hermes: sgme_memory_search
    dsh: memory_search
exemptions:
  append:
    hermes: sync_turn 每轮自动入库
    dsh: session-sync 自动入库
  skill_candidate_pending:
    hermes: 管理面审核队列
    dsh: 同上（管理面，非会话面能力）
  skill_candidate_adopt:
    hermes: 管理动作
    dsh: 同上（需 admin key 的管理动作）
"""


def _write_parity(repo: Path, text: str) -> Path:
    path = repo / cf.PARITY_MAP_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_deploy_repo(repo: Path, baseline_tools: list[str], deploy_tools: list[str]) -> Path:
    """假仓库基线（MCP 工具面）+ 假部署副本（只有 lib/index.js）。"""
    server = repo / "sgme"
    server.mkdir(parents=True, exist_ok=True)
    (server / "mcp_server.py").write_text(
        "\n".join('{"name": "%s", "description": "x"},' % t for t in baseline_tools),
        encoding="utf-8",
    )
    deploy = repo / "deployed" / "dsh-sgme"
    (deploy / "lib").mkdir(parents=True, exist_ok=True)
    (deploy / "lib" / "index.js").write_text(
        "\n".join('const d%d = { name: "%s" };' % (i, t) for i, t in enumerate(deploy_tools)),
        encoding="utf-8",
    )
    return deploy


def test_alias_map_only_reads_aliases_section(tmp_path):
    """别名只来自 aliases 段：exemptions 段里的 dsh 理由不得被当成别名映射。"""
    _write_parity(tmp_path, PARITY_WITH_BOTH_SECTIONS)
    assert cf.dsh_alias_map(tmp_path) == {"search": "memory_search"}


def test_alias_values_are_ascii_tool_names(tmp_path):
    """旧 bug 复现形态：豁免理由（中文）混进别名值 → 本用例必须抓出来。"""
    _write_parity(tmp_path, PARITY_WITH_BOTH_SECTIONS)
    aliases = cf.dsh_alias_map(tmp_path)
    for key, value in aliases.items():
        assert TOOL_RE.match(key), f"别名基准名不合法: {key!r}"
        assert TOOL_RE.match(value), f"别名值不是工具名（疑似豁免理由混入）: {value!r}"
    assert "同上" not in json.dumps(aliases, ensure_ascii=False)


def test_exempt_tools_read_from_parity_map(tmp_path):
    """永久豁免从 parity map 的 exemptions.<tool>.dsh 动态读取（不再写死两项）。"""
    _write_parity(tmp_path, PARITY_WITH_BOTH_SECTIONS)
    assert cf.dsh_exempt_tools(tmp_path) == {
        "append",
        "skill_candidate_pending",
        "skill_candidate_adopt",
    }


def test_exempt_tools_fallback_without_parity_map(tmp_path):
    """没有 parity map（极简夹具/未同步仓库）→ 退回内置两项兜底，不误报。"""
    assert cf.dsh_exempt_tools(tmp_path) == set(cf.DSH_EXEMPT_TOOLS)


def test_exempt_tools_empty_section_means_no_exemptions(tmp_path):
    """map 存在但没有 exemptions 段 → 空集（真源语义：不豁免任何工具）。"""
    _write_parity(tmp_path, "version: 1\naliases: {}\n")
    assert cf.dsh_exempt_tools(tmp_path) == set()


def test_deploy_contract_ignores_exempt_tools(tmp_path):
    """豁免工具不进契约：部署副本缺 append/skill_candidate_* 不算缺口。"""
    _write_parity(tmp_path, PARITY_WITH_BOTH_SECTIONS)
    deploy = _make_deploy_repo(
        tmp_path,
        ["append", "skill_candidate_pending", "skill_candidate_adopt", "search", "health"],
        ["memory_search", "health"],  # 别名生效；豁免工具全缺
    )
    assert cf.check_deploy_contract(tmp_path, deploy) == []


def test_deploy_contract_still_flags_real_missing_tool(tmp_path):
    """真缺工具照旧报缺口（豁免机制不能把真缺口一起吞掉）。"""
    _write_parity(tmp_path, PARITY_WITH_BOTH_SECTIONS)
    deploy = _make_deploy_repo(
        tmp_path,
        ["append", "skill_candidate_pending", "search", "health"],
        ["memory_search"],  # 缺 health（非豁免）
    )
    gaps = cf.check_deploy_contract(tmp_path, deploy)
    assert any("health" in g for g in gaps), gaps
    assert not any("skill_candidate" in g or "append" in g for g in gaps), gaps


def test_cli_json_structure_unchanged(tmp_path, monkeypatch, capsys):
    """"--json 结构不变：ok/findings/gaps/deploy/failed 五键（T-292 契约）。"""
    monkeypatch.setattr(cf, "check_skill_copies", lambda *a, **k: [])
    monkeypatch.setattr(cf, "check_dsh_bundle", lambda *a, **k: [])
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setenv(cf.DEPLOY_DIR_ENV, str(tmp_path / "_absent"))
    assert cf.main(["--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"ok", "findings", "gaps", "deploy", "failed"}
    assert payload["ok"] is True and payload["gaps"] == [] and payload["failed"] is False


def test_repo_exempt_tools_same_source_as_parity_gate():
    """仓库现状：本门禁的豁免口径必须与 adapter_parity 读同一份 map（同源，不写死）。"""
    cfg = ap.load_map(str(BASE / cf.PARITY_MAP_REL))
    expected = {
        tool for tool, per_adapter in (cfg.get("exemptions") or {}).items()
        if isinstance(per_adapter, dict) and per_adapter.get("dsh")
    }
    actual = cf.dsh_exempt_tools(BASE)
    assert actual == expected
    assert {"append", "agent_onboarding"} <= actual, "架构豁免丢失"
    assert {"skill_candidate_pending", "skill_candidate_list",
            "skill_candidate_adopt", "skill_candidate_review"} <= actual, "管理面豁免丢失"
    for name in actual:
        assert TOOL_RE.match(name), f"豁免清单里混入非工具名: {name!r}"


def test_repo_deploy_gaps_no_exempt_false_positive(monkeypatch):
    """仓库现状：部署缺口里不得再出现豁免工具或中文理由（假阳性回归锁）。"""
    deploy = cf.resolve_deploy_dir()
    if deploy is None:
        pytest.skip("本机无 dsh 部署副本（CI / 未装插件），部署态检查跳过")
    gaps = cf.check_deploy_contract(BASE, deploy)
    joined = "\n".join(gaps)
    assert "同上" not in joined, f"豁免理由被当成工具名: {gaps}"
    for exempt in cf.dsh_exempt_tools(BASE):
        assert exempt not in joined, f"豁免工具 {exempt} 被误报为部署缺口: {gaps}"


# ===========================================================================
# B. verify_decisions.py
# ===========================================================================

def _entry(did: str, doc: str, results: list[tuple[str, bool, str]]) -> dict:
    return {"id": did, "name": f"fixture {did}", "doc": doc, "fn": lambda _self: results}


def test_all_ids_unique_and_doc_status_valid():
    ids = [c["id"] for c in vd.CHECKS]
    assert len(ids) == len(set(ids)), "决策 id 重复"
    for c in vd.CHECKS:
        assert c["doc"] in ("done", "partial", "planned"), c
        assert re.fullmatch(r"D-\d+", c["id"]), c["id"]


def test_coverage_target_reached():
    """任务验收：覆盖 >= 40/73（防后续新增检查时倒退）。"""
    assert 40 <= len(vd.CHECKS) <= vd.TOTAL_DECISIONS, len(vd.CHECKS)


def test_every_check_returns_assertions():
    for c in vd.CHECKS:
        results = c["fn"](None)
        assert results, f"{c['id']} 没有断言"
        for item in results:
            assert len(item) == 3, f"{c['id']} 断言元组形状错误: {item!r}"


def test_done_and_partial_checks_currently_all_pass():
    """当前仓库必须零 MISMATCH：done/partial 的断言全过（否则门禁会红）。"""
    bad = []
    for c in vd.CHECKS:
        if c["doc"] not in ("done", "partial"):
            continue
        results = c["fn"](None)
        failed = [desc for desc, ok, _ in results if not ok]
        if failed:
            bad.append((c["id"], failed))
    assert not bad, f"以下决策断言失败: {bad}"


def test_planned_checks_currently_not_landed():
    """planned 项必须确实未落地（若全过则是「文档落后」，应改文档或改状态）。"""
    landed = []
    for c in vd.CHECKS:
        if c["doc"] != "planned":
            continue
        results = c["fn"](None)
        if all(ok for _, ok, _ in results):
            landed.append(c["id"])
    assert not landed, f"以下 planned 决策代码已落地（文档落后）: {landed}"


def test_cli_exit0_mismatch0_and_prints_coverage(capsys):
    rc = vd.main([])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "MISMATCH 0 条" in out, out
    assert re.search(r"覆盖 \d+/73 条决策", out), out
    assert "文档落后 0 条" in out, out


def test_strict_mode_also_green(capsys):
    """验收：--strict 也 EXIT=0（待施工是已登记缺口，不该让发布门禁常驻红）。"""
    rc = vd.main(["--strict"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "MISMATCH 0 条" in out
    assert "严格模式" not in out


def test_only_filter_prints_scope(capsys):
    rc = vd.main(["--only", "D-55"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "本次检查 1 条" in out, out
    assert "D-55" in out and "D-68" not in out, out


def test_mismatch_detected_for_done_when_assertion_fails(monkeypatch, capsys):
    monkeypatch.setattr(vd, "CHECKS", [
        _entry("D-901", "done", [("断言通过", True, "x"), ("断言失败", False, "x")]),
    ])
    assert vd.main([]) == 1
    assert "MISMATCH 1 条" in capsys.readouterr().out


def test_mismatch_detected_for_partial_when_landed_part_missing(monkeypatch, capsys):
    monkeypatch.setattr(vd, "CHECKS", [_entry("D-902", "partial", [("未落地", False, "x")])])
    assert vd.main([]) == 1
    assert "MISMATCH 1 条" in capsys.readouterr().out


def test_planned_fail_is_not_failure(monkeypatch, capsys):
    monkeypatch.setattr(vd, "CHECKS", [_entry("D-903", "planned", [("未落地", False, "x")])])
    assert vd.main([]) == 0
    out = capsys.readouterr().out
    assert "待施工 1 条" in out and "MISMATCH 0 条" in out


def test_planned_pass_warns_default_but_fails_strict(monkeypatch, capsys):
    """planned 断言全过 = 文档落后：默认只警告，--strict 升级为失败。"""
    monkeypatch.setattr(vd, "CHECKS", [_entry("D-904", "planned", [("已落地", True, "x")])])
    assert vd.main([]) == 0
    assert "文档落后 1 条" in capsys.readouterr().out
    assert vd.main(["--strict"]) == 1
    assert "严格模式" in capsys.readouterr().out


def test_d55_does_not_match_guardrail_read_mode(tmp_path, monkeypatch):
    """D-55 回归：guardrail 的 read_mode 不得被当成迁移切读开关（旧假阳性）。

    构造两版 config：①只有 guardrail read_mode → 必须不通过；②migration 段 +
    dual_write → 必须通过。这样锁死「断言的是迁移配置，不是任意 read_mode」。
    """
    d55 = next(c for c in vd.CHECKS if c["id"] == "D-55")
    cfg = tmp_path / "config.py"
    # ① 只有 guardrail 的 read_mode（旧实现会误判为迁移切读开关）
    cfg.write_text(
        'GUARDRAIL = {"read_mode": "strict"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(vd, "FILES", {**vd.FILES, "config": cfg})
    monkeypatch.setattr(vd, "_cache", {})
    assert not all(ok for _, ok, _ in d55["fn"](None)), "guardrail read_mode 又被误判为迁移开关"

    # ② 真正的迁移配置（migration 段 + dual_write）→ 通过
    cfg.write_text(
        'CONFIG = {"migration": {"dual_write": False, "read_mode": "legacy"}}\n'
        'GUARDRAIL = {"read_mode": "strict"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(vd, "_cache", {})
    assert all(ok for _, ok, _ in d55["fn"](None))


def test_assertion_message_has_file_path_and_missing_symbol():
    ok, msg = vd.has("db", "__definitely_missing_symbol__")
    assert ok is False
    assert "sgme/data/db.py" in msg, msg
    assert "__definitely_missing_symbol__" in msg, msg


def test_missing_file_message_has_path(monkeypatch):
    monkeypatch.setattr(vd, "FILES", {**vd.FILES, "ghost": Path("sgme/__no_such_file__.py")})
    monkeypatch.setattr(vd, "_cache", {})
    ok, msg = vd.has("ghost", "anything")
    assert ok is False
    assert "文件不存在" in msg and "sgme/__no_such_file__.py" in msg, msg
