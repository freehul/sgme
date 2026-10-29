# -*- coding: utf-8 -*-
"""tests/test_adapter_freshness.py：适配器分发新鲜度门禁测试（T-226 / T-227 防复发）。

分两类，各自目的不同：

A. 门禁机制测试（夹具驱动，永远可跑）——证明「陈旧派生产物必红」：
   - 分发副本被手改 / 真源改了副本没跟上 / 副本缺文件 / 多文件 / 整个缺失 → 检出
   - dsh lib 缺 src 新增的工具名（T-226 同款事故形态）→ 检出
   - dsh lib 多出 src 没有的工具名（产物比 src 新）→ 检出
   - 新增源文件未进产物（缺 //#region 标记）→ 检出
   - name 提取规则失效（读到 0 个）→ 失败而不是静默通过

B. 仓库现状测试（真实仓库）——两条防线当前必须全绿：
   八副本与真源重新打包结果逐文件一致；dsh lib 与 src 完全同步。

C. 部署态契约测试（T-292）——部署副本落后于仓库必须机器可见：
   - 部署副本缺基准工具（T-292 同款事故形态）→ 缺口项
   - 部署产物与仓库产物哈希不一致 → 缺口项
   - 契约齐 + 内容一致 → 零缺口
   - 无部署目录（CI / 未装插件）→ 跳过，不是失败
   - 基线解析失效 → fail-closed
   - 默认只警告、--strict-deploy 升级为失败（发布门禁口径）

D. 钩子挂点测试（挂点防丢）——`.githooks/pre-push` 必须真的调用本门禁：
   - 2026-10-29 补挂点：此前 pre-push 只查能力面对账，漂移可静默推上去，
     连续 5 次靠 CI 才发现。删挂点 / 改名 / 摘逃生开关 → 用例红。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts import check_adapter_freshness as cf  # noqa: E402

REAL_PUBLISH = BASE / "scripts" / "publish_adapter_skills.py"

# ---------------- A. 门禁机制（夹具） ----------------

@pytest.fixture
def fake_repo(tmp_path):
    """最小假仓库：一个 workbuddy 真源 + 真实 publish 脚本副本。"""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REAL_PUBLISH, tmp_path / "scripts" / "publish_adapter_skills.py")
    src = tmp_path / "adapters" / "workbuddy"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: sgme\ndescription: wb\n---\n# WB\n", encoding="utf-8"
    )
    (src / "scripts" / "client.py").write_text("VERSION = 1\n", encoding="utf-8")
    (src / "README.md").write_text("# readme\n", encoding="utf-8")
    return tmp_path


def _publish(repo: Path, host: str):
    """用真实 publish 逻辑在假仓库里发布一次（夹具用）。"""
    pub = cf._load_publish_module(repo)
    pub.ADAPTERS = repo / "adapters"
    pub.SKILLS = repo / "skills"
    r = pub.publish_host(host)
    assert r["ok"], r
    return repo / "skills" / f"adapter-{host}"


def test_fresh_copy_passes(fake_repo):
    """刚发布的分发副本必须零漂移（门禁基线行为）。"""
    _publish(fake_repo, "workbuddy")
    assert cf.check_skill_copies(fake_repo, hosts=["workbuddy"]) == []


def test_copy_content_tamper_detected(fake_repo):
    """分发副本被手改 → 内容过期。"""
    dest = _publish(fake_repo, "workbuddy")
    (dest / "scripts" / "client.py").write_text("VERSION = 999\n", encoding="utf-8")
    findings = cf.check_skill_copies(fake_repo, hosts=["workbuddy"])
    assert any("内容过期" in f and "client.py" in f for f in findings), findings


def test_source_bump_without_publish_detected(fake_repo):
    """真源改了、副本没重发（T-227 同款事故形态）→ 内容过期；重跑 publish 恢复绿。"""
    _publish(fake_repo, "workbuddy")
    (fake_repo / "adapters" / "workbuddy" / "scripts" / "client.py").write_text(
        "VERSION = 2\n", encoding="utf-8"
    )
    findings = cf.check_skill_copies(fake_repo, hosts=["workbuddy"])
    assert any("内容过期" in f for f in findings), findings

    _publish(fake_repo, "workbuddy")
    assert cf.check_skill_copies(fake_repo, hosts=["workbuddy"]) == []


def test_source_version_bump_without_publish_detected(fake_repo):
    """真源 plugin.yaml 版本升了副本没跟（P1-10 精确复刻）→ 检出。"""
    (fake_repo / "adapters" / "workbuddy" / "plugin.yaml").write_text(
        'name: sgme\nversion: "1.0.0"\n', encoding="utf-8"
    )
    _publish(fake_repo, "workbuddy")
    (fake_repo / "adapters" / "workbuddy" / "plugin.yaml").write_text(
        'name: sgme\nversion: "2.0.0"\n', encoding="utf-8"
    )
    findings = cf.check_skill_copies(fake_repo, hosts=["workbuddy"])
    assert any("内容过期" in f and "plugin.yaml" in f for f in findings), findings


def test_copy_missing_file_detected(fake_repo):
    """副本缺文件（真源有、副本无）→ 检出。"""
    dest = _publish(fake_repo, "workbuddy")
    (dest / "scripts" / "client.py").unlink()
    findings = cf.check_skill_copies(fake_repo, hosts=["workbuddy"])
    assert any("缺文件" in f and "client.py" in f for f in findings), findings


def test_copy_extra_file_detected(fake_repo):
    """副本多出真源没有的文件（手加的杂物）→ 检出。"""
    dest = _publish(fake_repo, "workbuddy")
    (dest / "junk_hand_added.md").write_text("x\n", encoding="utf-8")
    findings = cf.check_skill_copies(fake_repo, hosts=["workbuddy"])
    assert any("多文件" in f and "junk_hand_added.md" in f for f in findings), findings


def test_copy_missing_entirely_detected(fake_repo):
    """副本整个缺失 → 检出（提示重跑 publish）。"""
    findings = cf.check_skill_copies(fake_repo, hosts=["workbuddy"])
    assert any("分发副本缺失" in f for f in findings), findings


def test_line_ending_difference_tolerated(fake_repo):
    """行尾差异（git autocrlf 噪声）不算漂移——门禁跨行尾口径稳定，不报假警。"""
    dest = _publish(fake_repo, "workbuddy")
    f = dest / "scripts" / "client.py"
    lf_bytes = f.read_bytes().replace(b"\r\n", b"\n")  # 先归一拿到 LF 基线内容
    (fake_repo / "adapters" / "workbuddy" / "scripts" / "client.py").write_bytes(lf_bytes)
    _publish(fake_repo, "workbuddy")  # 真源与副本同为 LF
    f.write_bytes(lf_bytes.replace(b"\n", b"\r\n"))  # 只把副本改成 CRLF
    assert cf.check_skill_copies(fake_repo, hosts=["workbuddy"]) == []


def test_missing_source_host_detected(fake_repo):
    """真源适配器目录缺失（默认全量七宿主时）→ 打包失败也必须红。"""
    findings = cf.check_skill_copies(fake_repo)  # 默认七宿主，只有 workbuddy 存在
    assert any("真源无法打包" in f for f in findings), findings


# ---- dsh bundle 机制 ----

def _make_bridge(repo: Path, src: dict[str, str], lib_js: str, lib_dts: str = "export {};\n"):
    bridge = repo / "adapters" / "dsh" / "sgme-bridge"
    (bridge / "src").mkdir(parents=True)
    (bridge / "lib").mkdir()
    for name, text in src.items():
        (bridge / "src" / name).write_text(text, encoding="utf-8")
    (bridge / "lib" / "index.js").write_text(lib_js, encoding="utf-8")
    (bridge / "lib" / "index.d.ts").write_text(lib_dts, encoding="utf-8")
    return bridge


SRC_TWO_TOOLS = (
    "defineTool({ name: 'memory_search' })\n"
    "defineTool({ name: 'conversation_search' })\n"
)
LIB_FRESH = (
    "//#region src/tools.ts\n"
    'const a = { name: "memory_search" };\n'
    'const b = { name: "conversation_search" };\n'
)


def test_bundle_fresh_passes(tmp_path):
    """lib 与 src 同步 → 零漂移。"""
    _make_bridge(tmp_path, {"tools.ts": SRC_TWO_TOOLS}, LIB_FRESH)
    assert cf.check_dsh_bundle(tmp_path) == []


def test_bundle_stale_missing_tool_detected(tmp_path):
    """T-226 同款：src 加了 conversation_search，lib 是旧产物 → 必红。"""
    stale_lib = LIB_FRESH.replace('const b = { name: "conversation_search" };\n', "")
    _make_bridge(tmp_path, {"tools.ts": SRC_TWO_TOOLS}, stale_lib)
    findings = cf.check_dsh_bundle(tmp_path)
    assert any("conversation_search" in f and "忘了" in f for f in findings), findings


def test_bundle_lib_extra_tool_detected(tmp_path):
    """产物比 src 新（src 删了工具没重建）→ 也要红。"""
    lib = LIB_FRESH + 'const c = { name: "ghost_tool" };\n'
    _make_bridge(tmp_path, {"tools.ts": SRC_TWO_TOOLS}, lib)
    findings = cf.check_dsh_bundle(tmp_path)
    assert any("ghost_tool" in f and "lib 多" in f for f in findings), findings


def test_bundle_missing_region_detected(tmp_path):
    """新增源文件未进产物（缺 region 标记）→ 检出。"""
    _make_bridge(
        tmp_path,
        {"tools.ts": SRC_TWO_TOOLS, "newmod.ts": "export const x = 1;\n"},
        LIB_FRESH,
    )
    findings = cf.check_dsh_bundle(tmp_path)
    assert any("//#region src/newmod.ts" in f for f in findings), findings


def test_bundle_empty_src_fails_closed(tmp_path):
    """name 提取失效（src 读到 0 个）→ 失败而不是静默通过。"""
    _make_bridge(tmp_path, {"tools.ts": "// empty\n"}, LIB_FRESH)
    findings = cf.check_dsh_bundle(tmp_path)
    assert any("解析到 0 个 name" in f for f in findings), findings


def test_bundle_missing_lib_detected(tmp_path):
    """lib 产物缺失 → 检出。"""
    bridge = tmp_path / "adapters" / "dsh" / "sgme-bridge"
    (bridge / "src").mkdir(parents=True)
    (bridge / "src" / "tools.ts").write_text(SRC_TWO_TOOLS, encoding="utf-8")
    findings = cf.check_dsh_bundle(tmp_path)
    assert any("缺 lib/index.js 产物" in f for f in findings), findings


# ---------------- B. 仓库现状（真实仓库） ----------------

def test_repo_skill_copies_fresh():
    """八个 skills/adapter-* 必须与「真源重新打包」结果逐文件一致（T-227 防复发）。"""
    findings = cf.check_skill_copies(BASE)
    assert findings == [], "分发副本漂移：\n" + "\n".join(findings)


def test_repo_dsh_bundle_fresh():
    """dsh lib 产物必须与 src 同步（T-226 防复发）。"""
    findings = cf.check_dsh_bundle(BASE)
    assert findings == [], "dsh 产物漂移：\n" + "\n".join(findings)


def test_repo_cli_passes(tmp_path, monkeypatch):
    """CLI 入口接线正确（门禁可当命令独立跑）。

    把 home 与部署目录环境变量都指向不存在的路径：仓库侧判定必须与
    「本机是否装了插件」彻底解耦——否则同一份代码在不同机器上红绿不同
    （门禁可信度归零）。
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setenv(cf.DEPLOY_DIR_ENV, str(tmp_path / "_no_such_deploy"))
    assert cf.main(["--root", str(BASE)]) == 0


# ---------------- D. 钩子挂点（T-227 挂点防丢，2026-10-29） ----------------

HOOK = BASE / ".githooks" / "pre-push"


def test_pre_push_mounts_freshness_gate():
    """pre-push 必须挂新鲜度门禁，且带逃生开关。

    事故形态（2026-10-26 dd5f9a6 → 2026-10-29 才发现）：真源 SKILL.md 改了、
    分发副本没跟上，而 pre-push 当时只查能力面对账（T-170），连续 5 次 push
    全都放行，靠 CI 每次跑 9 分钟才发现红。挂点被删 / 脚本改名 / 逃生开关被摘，
    本用例必须红——否则同样的静默期会再来一次。
    """
    assert HOOK.is_file(), "缺 .githooks/pre-push（新克隆需 sh scripts/install_git_hooks.sh）"
    text = HOOK.read_text(encoding="utf-8")
    assert "scripts/check_adapter_freshness.py" in text, "pre-push 未调用新鲜度门禁"
    assert "GIT_PUSH_SKIP_FRESHNESS" in text, "缺逃生开关 GIT_PUSH_SKIP_FRESHNESS（留痕）"
    assert "exit 1" in text, "命中漂移必须阻断推送（缺 exit 1）"

# ---------------- C. 部署态契约（防线 3，T-292） ----------------

BASELINE_TOOLS = ["append", "search", "health", "credential_list"]
CONTRACT_TOOLS = ["search", "health", "credential_list"]  # 去掉架构豁免 append


def _make_deploy(repo: Path, tools: list[str], *, repo_tools=None, dup: bool = False) -> Path:
    """造一个假仓库基线（MCP 工具面）+ 假部署副本。

    repo_tools=None 时基线 = BASELINE_TOOLS；dup=True 让部署产物同时存在于 src 侧，
    便于构造「部署与仓库产物哈希一致」的场景。
    """
    server = repo / "sgme"
    server.mkdir(parents=True, exist_ok=True)
    (server / "mcp_server.py").write_text(
        "\n".join('{"name": "%s", "description": "x"},' % t for t in (repo_tools or BASELINE_TOOLS)),
        encoding="utf-8",
    )
    deploy = repo / "deployed" / "dsh-sgme"
    (deploy / "lib").mkdir(parents=True, exist_ok=True)
    text = "\n".join('const d%d = { name: "%s" };' % (i, t) for i, t in enumerate(tools)) + "\n"
    (deploy / "lib" / "index.js").write_text(text, encoding="utf-8")
    return deploy


def test_deploy_dir_missing_is_skipped(tmp_path, monkeypatch):
    """没有部署目录 = 跳过（CI / 未装插件的机器），不是失败。

    home 也指向空目录：默认候选是 ~/.dsh/...，不隔离 home 的话本机会命中
    真实 profile，用例就变成「测本机装没装插件」。
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    assert cf.resolve_deploy_dir(str(tmp_path / "nope")) is None
    assert cf.check_deploy_contract(tmp_path, None) == []


def test_deploy_contract_stale_tool_detected(tmp_path):
    """T-292 同款：部署副本缺基准工具 → 缺口项（而不是静默全绿）。"""
    deploy = _make_deploy(tmp_path, ["search", "health"])  # 缺 credential_list
    gaps = cf.check_deploy_contract(tmp_path, deploy)
    assert any("credential_list" in g for g in gaps), gaps


def test_deploy_contract_fresh_is_clean(tmp_path):
    """部署产物含全部契约工具且与仓库产物一致 → 零缺口。"""
    deploy = _make_deploy(tmp_path, CONTRACT_TOOLS)
    # 让「仓库产物」与部署产物逐字节一致（含相同注释头也无妨，比对的是整体哈希）
    repo_bridge = tmp_path / cf.BRIDGE_REL / "lib"
    repo_bridge.mkdir(parents=True)
    (repo_bridge / "index.js").write_bytes((deploy / "lib" / "index.js").read_bytes())
    assert cf.check_deploy_contract(tmp_path, deploy) == []


def test_deploy_digest_drift_detected(tmp_path):
    """契约齐但内容与仓库产物不一致（部署副本内容漂移）→ 检出。"""
    deploy = _make_deploy(tmp_path, CONTRACT_TOOLS)
    repo_bridge = tmp_path / cf.BRIDGE_REL / "lib"
    repo_bridge.mkdir(parents=True)
    (repo_bridge / "index.js").write_text("// stale\n", encoding="utf-8")
    gaps = cf.check_deploy_contract(tmp_path, deploy)
    assert any("内容不一致" in g for g in gaps), gaps


def test_deploy_baseline_unparsable_fails_closed(tmp_path):
    """基线解析到 0 个工具（提取口径失效）→ fail-closed，不静默通过。"""
    deploy = tmp_path / "deployed" / "dsh-sgme"
    (deploy / "lib").mkdir(parents=True)
    (deploy / "lib" / "index.js").write_text('const a = { name: "x" };\n', encoding="utf-8")
    gaps = cf.check_deploy_contract(tmp_path, deploy)
    assert any("fail-closed" in g for g in gaps), gaps


def test_deploy_alias_map_read_from_parity_map(tmp_path):
    """别名（基准 search ↔ dsh memory_search）从 parity map 读，不重复声明。"""
    parity = tmp_path / cf.PARITY_MAP_REL
    parity.parent.mkdir(parents=True, exist_ok=True)
    parity.write_text(
        "aliases:\n  search:\n    hermes: sgme_memory_search\n    dsh: memory_search\n",
        encoding="utf-8",
    )
    assert cf.dsh_alias_map(tmp_path) == {"search": "memory_search"}
    deploy = _make_deploy(tmp_path, ["memory_search", "health", "credential_list"])
    assert not any("search" in g for g in cf.check_deploy_contract(tmp_path, deploy))


def test_deploy_strict_flag_escalates(fake_repo, tmp_path, monkeypatch):
    """默认只警告（exit 0）；--strict-deploy 让部署缺口失败（发布门禁）。

    两道仓库防线打桩为「零漂移」，让用例只考察「部署缺口是否升级为失败」
    这一件事（其余噪声由 A/B 区用例各自负责）。
    """
    monkeypatch.setattr(cf, "check_skill_copies", lambda *a, **k: [])
    monkeypatch.setattr(cf, "check_dsh_bundle", lambda *a, **k: [])
    _make_deploy(fake_repo, ["search", "health"])  # 缺 credential_list
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setenv(cf.DEPLOY_DIR_ENV, str(fake_repo / "deployed" / "dsh-sgme"))
    assert cf.main(["--root", str(fake_repo)]) == 0
    assert cf.main(["--root", str(fake_repo), "--strict-deploy"]) == 1


def test_deploy_check_skipped_without_dir(fake_repo, monkeypatch):
    """部署候选都不存在 → 跳过部署检查（env 优先，回落默认目录）。"""
    monkeypatch.setattr(Path, "home", lambda: fake_repo / "nohome")
    monkeypatch.setenv(cf.DEPLOY_DIR_ENV, str(fake_repo / "absent"))
    res = cf.check(fake_repo)
    assert res["deploy"] is None
    assert res["gaps"] == []
