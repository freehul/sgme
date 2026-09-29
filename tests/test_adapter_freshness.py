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
   六副本与真源重新打包结果逐文件一致；dsh lib 与 src 完全同步。
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
    """真源适配器目录缺失（默认全量六宿主时）→ 打包失败也必须红。"""
    findings = cf.check_skill_copies(fake_repo)  # 默认六宿主，只有 workbuddy 存在
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
    """六个 skills/adapter-* 必须与「真源重新打包」结果逐文件一致（T-227 防复发）。"""
    findings = cf.check_skill_copies(BASE)
    assert findings == [], "分发副本漂移：\n" + "\n".join(findings)


def test_repo_dsh_bundle_fresh():
    """dsh lib 产物必须与 src 同步（T-226 防复发）。"""
    findings = cf.check_dsh_bundle(BASE)
    assert findings == [], "dsh 产物漂移：\n" + "\n".join(findings)


def test_repo_cli_passes():
    """CLI 入口接线正确（门禁可当命令独立跑）。"""
    assert cf.main(["--root", str(BASE)]) == 0
