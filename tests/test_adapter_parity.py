# -*- coding: utf-8 -*-
"""tests/test_adapter_parity.py：官方适配器能力面对账门禁测试（T-170 / ST-42）。

分两类，各自目的不同：

A. 门禁机制测试（夹具驱动，永远可跑）
   用临时目录造假的基准文件与三个假适配器，验证判定逻辑与纪律：
   - 未声明的缺口 = 漂移（失败）
   - 豁免 / 直通 / 待补齐 三种声明能放行，且各自要求写理由、写通道、写任务号
   - 声明腐烂（pending 已实现、豁免已实现）= 提示清理
   - 解析规则失效（读到 0 个符号）= 失败，防止门禁静默失效
   - --strict 下待补齐也算失败（发布门禁）

B. 仓库现状测试（真实八适配器）
   断言当前仓库八适配器对基准工具面无未声明漂移，并单独盯住历史断点
   （Hermes 的技能层读侧工具），防复发。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from scripts import adapter_parity as ap  # noqa: E402


# ---------------- A. 门禁机制（夹具） ----------------

BASELINE_TXT = '\n'.join(
    '    {"name": "%s", "description": "x"},' % n for n in
    ["alpha", "beta", "gamma", "delta"]
)

HERMES_TXT = "\n".join([
    '{"name": "sgme_alpha", "description": "x"},',
    '{"name": "sgme_beta", "description": "x"},',
])

DSH_TXT = "\n".join([
    '\t\tname: "alpha",',
    '\t\tname: "beta",',
    '\t\tname: "gamma",',
    '\t\tname: "delta",',
])

DOUBAO_TXT = "\n".join([
    "class C:",
    "    def alpha(self):",
    "        pass",
    "    def beta(self):",
    "        pass",
    "    def _helper(self):",
    "        pass",
])

DYNAMIC_TXT = "\n".join([
    "class Proxy:",
    "    def list_tools(self):",
    "        return self.remote.list_tools()",
    "    def call_tool(self, name, arguments):",
    "        return self.remote.call_tool(name, arguments)",
])


def _fixture_map(tmp_path, *, exemptions=None, passthrough=None, pending=None, extra=None):
    """在临时目录造基准 + 三个假适配器，返回对应的 cfg 字典。"""
    files = {
        "base.py": BASELINE_TXT,
        "hermes.py": HERMES_TXT,
        "dsh.js": DSH_TXT,
        "doubao.py": DOUBAO_TXT,
    }
    paths = {}
    for name, text in files.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        paths[name] = str(p)
    return {
        "baseline": {"file": paths["base.py"], "pattern": r'\{"name":\s*"([a-z_]+)"'},
        "adapters": {
            "hermes": {"file": paths["hermes.py"], "pattern": r'"name":\s*"([a-z_]+)"',
                       "strip_prefix": "sgme_"},
            "dsh": {"file": paths["dsh.js"], "pattern": r'name:\s*"([a-z_]+)"'},
            "doubao": {"file": paths["doubao.py"], "pattern": r"^    def ([a-z_]+)\(",
                       "exclude": ["_helper"]},
        },
        "aliases": {},
        "exemptions": exemptions or {},
        "passthrough": passthrough or {},
        "extra": extra or {},
        "pending": pending or {},
    }


def _add_dynamic_adapter(cfg, tmp_path, *, reason="动态透传远端 MCP 工具面", patterns=None):
    path = tmp_path / "codex.py"
    path.write_text(DYNAMIC_TXT, encoding="utf-8")
    cfg["adapters"]["codex"] = {
        "file": str(path),
        "dynamic_forwarder": {
            "reason": reason,
            "require_patterns": patterns
            or [r"remote\.list_tools\(\)", r"remote\.call_tool\("],
        },
    }
    return cfg


def test_dynamic_forwarder_covers_all_baseline_tools(tmp_path):
    cfg = _add_dynamic_adapter(_fixture_map(tmp_path), tmp_path)

    res = ap.check(cfg)

    assert res["stats"]["codex"]["forwarded"] == len(res["baseline_tools"])
    assert all(
        res["matrix"][tool]["codex"]["state"] == ap.FORWARDED
        for tool in res["baseline_tools"]
    )
    assert not any("codex" in error for error in res["errors"])


def test_dynamic_forwarder_requires_reason(tmp_path):
    cfg = _add_dynamic_adapter(_fixture_map(tmp_path), tmp_path, reason="")

    res = ap.check(cfg)

    assert any("动态转发器 codex 缺少 reason" in error for error in res["errors"])


def test_dynamic_forwarder_requires_guard_patterns(tmp_path):
    cfg = _add_dynamic_adapter(
        _fixture_map(tmp_path), tmp_path, patterns=[r"remote\.not_there\("]
    )

    res = ap.check(cfg)

    assert any("动态转发器 codex 缺少透传锚点" in error for error in res["errors"])


def test_undeclared_gap_is_drift(tmp_path):
    """未声明的缺口必须判为漂移（这是门禁存在的理由）。"""
    res = ap.check(_fixture_map(tmp_path))
    assert res["ok"] is False
    drifts = {t for t in res["baseline_tools"] for ad in res["adapters"]
              if res["matrix"][t][ad]["state"] == ap.DRIFT}
    # hermes 缺 gamma/delta；doubao 缺 gamma/delta（_helper 是内部方法不计）
    assert drifts == {"gamma", "delta"}


def test_exemption_needs_reason_and_passes(tmp_path):
    """写了理由的豁免放行；豁免本身不带理由字段时由 YAML 约束（值为字符串）。"""
    res = ap.check(_fixture_map(tmp_path, exemptions={
        "gamma": {"hermes": "内部自动处理，无工具落点"},
        "delta": {"hermes": "同上理由"},
    }))
    assert res["matrix"]["gamma"]["hermes"]["state"] == ap.EXEMPT
    # dsh / doubao 仍有 gamma/delta 漂移 → 整体仍失败
    assert res["ok"] is False
    assert all("hermes" not in e for e in res["errors"])


def test_passthrough_needs_channel(tmp_path):
    """通配通道声明放行，通道名进明细。"""
    res = ap.check(_fixture_map(tmp_path, passthrough={
        "doubao": {"channel": "mcp_raw", "tools": ["gamma", "delta"], "reason": "按需直调"},
    }))
    assert res["matrix"]["gamma"]["doubao"]["state"] == ap.PASSTHROUGH
    assert "mcp_raw" in res["matrix"]["gamma"]["doubao"]["detail"]


def test_pending_requires_task_number(tmp_path):
    """待补齐必须挂任务号——防「待补齐」变成不挂账的黑洞。"""
    bad = ap.check(_fixture_map(tmp_path, pending={"hermes": {"tools": ["gamma", "delta"]},
                                                   "doubao": {"task": "T-999", "tools": ["gamma", "delta"]}}))
    assert bad["ok"] is False
    assert any("缺少任务号" in e for e in bad["errors"])

    good = ap.check(_fixture_map(tmp_path, pending={
        "hermes": {"task": "T-999", "tools": ["gamma", "delta"]},
        "doubao": {"task": "T-999", "tools": ["gamma", "delta"]},
    }))
    assert good["matrix"]["gamma"]["hermes"]["state"] == ap.PENDING
    assert good["pending_count"] == 4
    assert good["ok"] is True  # 非严格模式：已登记待补齐不算失败


def test_stale_pending_is_flagged(tmp_path):
    """已实现却仍挂在待补齐 → 提示清理，防声明腐烂。"""
    res = ap.check(_fixture_map(tmp_path, pending={
        "hermes": {"task": "T-999", "tools": ["alpha", "gamma", "delta"]},
    }))
    assert any("请从 pending 移除" in w for w in res["warnings"])


def test_stale_exemption_is_flagged(tmp_path):
    res = ap.check(_fixture_map(tmp_path, exemptions={
        "alpha": {"hermes": "已不需要的旧豁免"},
    }))
    assert any("请删除声明" in w for w in res["warnings"])


def test_unparsable_adapter_fails(tmp_path):
    """解析规则失效（读到 0 个符号）必须失败——否则门禁会静默通过一切。"""
    cfg = _fixture_map(tmp_path)
    cfg["adapters"]["hermes"]["pattern"] = r'"NOPE":\s*"([a-z_]+)"'
    res = ap.check(cfg)
    assert res["ok"] is False
    assert any("读不到适配器能力面" in e for e in res["errors"])


def test_bad_alias_target_fails(tmp_path):
    """别名指向不存在的符号 = 映射漂移，必须失败。"""
    cfg = _fixture_map(tmp_path)
    cfg["aliases"] = {"alpha": {"hermes": "sgme_not_a_real_symbol"}}
    res = ap.check(cfg)
    assert res["ok"] is False
    assert any("不存在" in e for e in res["errors"])


def test_unknown_tool_in_declaration_fails(tmp_path):
    """声明里出现基准面之外的工具名 = 基准已改名/删工具，必须失败。"""
    cfg = _fixture_map(tmp_path)
    cfg["exemptions"] = {"ghost_tool": {"hermes": "x"}}
    res = ap.check(cfg)
    assert res["ok"] is False
    assert any("不在基准工具面" in e for e in res["errors"])


def test_strict_mode_blocks_pending(tmp_path):
    """--strict（发布门禁）下待补齐同样失败。"""
    cfg = _fixture_map(tmp_path, pending={"hermes": {"task": "T-999", "tools": ["gamma", "delta"]},
                                          "doubao": {"task": "T-999", "tools": ["gamma", "delta"]}})
    assert ap.main([], cfg=cfg) == 0            # 非严格：放行
    assert ap.main(["--strict"], cfg=cfg) == 1  # 严格：拦下


# ---------------- B. 仓库现状（真实八适配器） ----------------

@pytest.fixture(scope="module")
def repo_result():
    return ap.check()


def test_repo_adapters_parse_nonempty(repo_result):
    """八个适配器都要能解析出能力面（防解析规则随重构失效）。"""
    for ad, st in repo_result["stats"].items():
        assert (
            st["covered"]
            + st["forwarded"]
            + st["exempt"]
            + st["passthrough"]
            + st["pending"]
            > 0
        ), f"{ad} 解析为空"


def test_repo_no_undeclared_drift(repo_result):
    """当前仓库八适配器对基准工具面无未声明漂移。"""
    assert repo_result["errors"] == [], "存在未声明漂移：\n" + "\n".join(repo_result["errors"])


def test_repo_no_pending_debt(repo_result):
    """待补齐必须清零（全补齐是本任务的验收标准，不许长期挂账）。"""
    assert repo_result["pending_count"] == 0, "仍有待补齐项，见 adapter_parity_map.yaml 的 pending"


def test_repo_codex_dynamic_forwarder_covers_all_tools(repo_result):
    """Codex 是动态 MCP 代理，必须以源码锚点证明完整透传而非静态工具清单。"""
    assert "codex" in repo_result["adapters"]
    assert repo_result["stats"]["codex"]["forwarded"] == repo_result["baseline_count"]
    assert all(
        repo_result["matrix"][tool]["codex"]["state"] == ap.FORWARDED
        for tool in repo_result["baseline_tools"]
    )


def test_repo_claude_code_dynamic_forwarder_covers_all_tools(repo_result):
    """Claude Code 同为动态 MCP 代理，必须以源码锚点证明完整透传而非静态工具清单。"""
    assert "claude-code" in repo_result["adapters"]
    assert repo_result["stats"]["claude-code"]["forwarded"] == repo_result["baseline_count"]
    assert all(
        repo_result["matrix"][tool]["claude-code"]["state"] == ap.FORWARDED
        for tool in repo_result["baseline_tools"]
    )


def test_hermes_covers_skills_read_side(repo_result):
    """历史断点回归：Hermes 必须能检索技能层并取技能全文（技能共享的实际卡点）。"""
    for tool in ("skill_search", "skill_digest", "skill_get", "skill_materialize"):
        st = repo_result["matrix"][tool]["hermes"]["state"]
        assert st == ap.COVERED, f"hermes 缺失 {tool}（{st}）"


def test_all_official_adapters_stay_level(repo_result):
    """官方适配器平级：除显式豁免/直通外，有效覆盖率必须一致。"""
    ads = repo_result["adapters"]
    eff = {ad: repo_result["stats"][ad]["covered"] + repo_result["stats"][ad]["exempt"]
           + repo_result["stats"][ad]["passthrough"] + repo_result["stats"][ad]["forwarded"]
           for ad in ads}
    assert len(set(eff.values())) == 1, f"适配器能力面不一致：{eff}"


# ---------------- C. CLI 环境兼容（T-239：GBK 控制台加固） ----------------

def test_cli_strict_survives_gbk_stdout():
    """GBK 控制台/管道（Windows 默认代码页）下 --strict 输出不因 ✓ 崩溃（T-239）。

    复现路径：PYTHONIOENCODING=gbk 时 render() 输出的 ✓ 无法编码，
    修前抛 UnicodeEncodeError；修复后在 CLI 入口做 errors=replace 兜底。
    """
    script = os.path.join(BASE, "scripts", "adapter_parity.py")
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    proc = subprocess.run(
        [sys.executable, script, "--strict"],
        capture_output=True, env=env, cwd=BASE,
    )
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstderr=" + proc.stderr.decode("utf-8", "replace")
    )
